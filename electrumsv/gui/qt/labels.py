# ElectrumSV - lightweight Bitcoin client
# Copyright (C) 2014 Thomas Voegtlin
# Copyright (C) 2019 ElectrumSV developers
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import base64
from functools import partial
import json
import hashlib
import requests
import threading

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QVBoxLayout)

from electrumsv.crypto import aes_decrypt_with_iv, aes_encrypt_with_iv
from electrumsv.i18n import _
from electrumsv.logs import logs
from electrumsv.plugin import BasePlugin, hook

from electrumsv.gui.qt.util import (
    ThreadedButton, Buttons, EnterButton, WindowModalDialog, OkButton,
)


logger = logs.get_logger("labels")


class Labels(object):

    def __init__(self, parent, config, name):
        BasePlugin.__init__(self, parent, config, name)
        self.target_host = 'labels.bauerj.eu'
        self.wallets = {}

    def encode(self, wallet, msg):
        password, iv, wallet_id = self.wallets[wallet]
        encrypted = aes_encrypt_with_iv(password, iv, msg.encode('utf8'))
        return base64.b64encode(encrypted).decode()

    def decode(self, wallet, message):
        password, iv, wallet_id = self.wallets[wallet]
        decoded = base64.b64decode(message)
        decrypted = aes_decrypt_with_iv(password, iv, decoded)
        return decrypted.decode('utf8')

    def get_nonce(self, wallet):
        # nonce is the nonce to be used with the next change
        nonce = wallet.storage.get('wallet_nonce')
        if nonce is None:
            nonce = 1
            self.set_nonce(wallet, nonce)
        return nonce

    def set_nonce(self, wallet, nonce):
        logger.debug("set %s nonce to %s", wallet.basename(), nonce)
        wallet.storage.put("wallet_nonce", nonce)

    @hook
    def set_label(self, wallet, item, label):
        if not wallet in self.wallets:
            return
        if not item:
            return
        nonce = self.get_nonce(wallet)
        wallet_id = self.wallets[wallet][2]
        bundle = {"walletId": wallet_id,
                  "walletNonce": nonce,
                  "externalId": self.encode(wallet, item),
                  "encryptedLabel": self.encode(wallet, label)}
        t = threading.Thread(target=self.do_request,
                             args=["POST", "/label", False, bundle])
        t.setDaemon(True)
        t.start()
        # Caller will write the wallet
        self.set_nonce(wallet, nonce + 1)

    def do_request(self, method, url = "/labels", is_batch=False, data=None):
        url = 'https://' + self.target_host + url
        kwargs = {'headers': {}}
        if method == 'GET' and data:
            kwargs['params'] = data
        elif method == 'POST' and data:
            kwargs['data'] = json.dumps(data)
            kwargs['headers']['Content-Type'] = 'application/json'
        response = requests.request(method, url, **kwargs)
        if response.status_code != 200:
            raise BaseException(response.status_code, response.text)
        response = response.json()
        if "error" in response:
            raise BaseException(response["error"])
        return response

    def push_thread(self, wallet):
        wallet_id = self.wallets[wallet][2]
        bundle = {"labels": [],
                  "walletId": wallet_id,
                  "walletNonce": self.get_nonce(wallet)}
        for key, value in wallet.labels.items():
            try:
                encoded_key = self.encode(wallet, key)
                encoded_value = self.encode(wallet, value)
            except:
                logger.error('cannot encode %r %r', key, value)
                continue
            bundle["labels"].append({'encryptedLabel': encoded_value,
                                     'externalId': encoded_key})
        self.do_request("POST", "/labels", True, bundle)

    def pull_thread(self, wallet, force):
        wallet_id = self.wallets[wallet][2]
        nonce = 1 if force else self.get_nonce(wallet) - 1
        logger.debug("asking for labels since nonce %s", nonce)
        try:
            response = self.do_request("GET", ("/labels/since/%d/for/%s" % (nonce, wallet_id) ))
            if response["labels"] is None:
                logger.debug('no new labels')
                return
            result = {}
            for label in response["labels"]:
                try:
                    key = self.decode(wallet, label["externalId"])
                    value = self.decode(wallet, label["encryptedLabel"])
                except:
                    continue
                try:
                    json.dumps(key)
                    json.dumps(value)
                except:
                    logger.error('no json %s', key)
                    continue
                result[key] = value

            for key, value in result.items():
                if force or not wallet.labels.get(key):
                    wallet.labels[key] = value

            logger.debug("received %d labels", len(response))
            # do not write to disk because we're in a daemon thread
            wallet.storage.put('labels', wallet.labels)
            self.set_nonce(wallet, response["nonce"] + 1)
            self.on_pulled(wallet)
        except Exception as e:
            logger.exception("could not retrieve labels")

    def on_pulled(self, _wallet):
        raise NotImplementedError()

    def start_wallet(self, wallet):
        nonce = self.get_nonce(wallet)
        logger.debug("wallet %s nonce is %s", wallet.basename(), nonce)
        mpk = wallet.get_fingerprint()
        if not mpk:
            return
        mpk = mpk.encode('ascii')
        password = hashlib.sha1(mpk).hexdigest()[:32].encode('ascii')
        iv = hashlib.sha256(password).digest()[:16]
        wallet_id = hashlib.sha256(mpk).hexdigest()
        self.wallets[wallet] = (password, iv, wallet_id)
        # If there is an auth token we can try to actually start syncing
        t = threading.Thread(target=self.pull_thread, args=(wallet, False))
        t.setDaemon(True)
        t.start()

    def stop_wallet(self, wallet):
        self.wallets.pop(wallet, None)




class QLabelsSignalObject(QObject):
    labels_changed_signal = pyqtSignal(object)


class Plugin(LabelsPlugin):

    def __init__(self, *args):
        LabelsPlugin.__init__(self, *args)
        self.obj = QLabelsSignalObject()

    def requires_settings(self):
        return True

    def settings_widget(self, window):
        return EnterButton(_('Settings'),
                           partial(self.settings_dialog, window))

    def settings_dialog(self, window):
        wallet = window.parent().wallet
        d = WindowModalDialog(window, _("Label Settings"))
        hbox = QHBoxLayout()
        hbox.addWidget(QLabel("Label sync options:"))
        upload = ThreadedButton("Force upload",
                                partial(self.push_thread, wallet),
                                partial(self.done_processing, d))
        download = ThreadedButton("Force download",
                                  partial(self.pull_thread, wallet, True),
                                  partial(self.done_processing, d))
        vbox = QVBoxLayout()
        vbox.addWidget(upload)
        vbox.addWidget(download)
        hbox.addLayout(vbox)
        vbox = QVBoxLayout(d)
        vbox.addLayout(hbox)
        vbox.addSpacing(20)
        vbox.addLayout(Buttons(OkButton(d)))
        return bool(d.exec_())

    def on_pulled(self, wallet):
        self.obj.labels_changed_signal.emit(wallet)

    def done_processing(self, dialog, result):
        dialog.show_message(_("Your labels have been synchronised."))

    @hook
    def on_new_window(self, window):
        self.obj.labels_changed_signal.connect(window.update_tabs)
        self.start_wallet(window.wallet)

    @hook
    def on_close_window(self, window):
        self.stop_wallet(window.wallet)