#!/usr/bin/env python

"""
NOTE:
There are a few places in this script that disable certificate/hostname checks.
To improve the security of transport, be sure to use properly signed
certificates and remove:

`r = requests.post('https://%s/%s' % (cmd_args.telemetryUrl, AUTH_PATH),
                   data=json.dumps(credentials), headers=headers,
                   verify=False)`

as well as

`connection.socket.run_forever(sslopt={'check_hostname': False,
                                      'cert_reqs': ssl.CERT_NONE})`

"""

import argparse
from Crypto.Hash import SHA256
from email.mime.text import MIMEText
import getpass
import json
import logging
import random
import requests
import string
import smtplib
import ssl
import websocket
import threading
import time

VERSION_09 = '0.9.0'
VERSION_1 = '1.0.0'
SUBSCRIBE = 'subscribe'
GET = 'get'

AUTH_PATH = 'cvpservice/login/authenticate.do'


class TelemetryWs(object):
    """
    Class to handle connection methods required to get
    and subscribe to steaming data.
    """

    def __init__(self, cmd_args, passwords):
        super(TelemetryWs, self).__init__()

        if cmd_args.noTelemetrySSL:
            telemetry_ws = 'ws://{}/aeris/v1/wrpc/'.format(cmd_args.telemetryUrl)
            self.socket = websocket.WebSocketApp(
                telemetry_ws,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
            )
        else:  # login and setup wss
            credentials = {
                'userId': cmd_args.telemetryUsername,
                'password': passwords['telemetryPassword'],
            }
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json',
            }
            request = requests.post(
                'https://{}/{}'.format(cmd_args.telemetryUrl, AUTH_PATH),
                data=json.dumps(credentials), headers=headers,
                verify=False,
            )

            if request.status_code == 200:
                logging.info('Successfully logged in to Telemetry.')
                headers = [
                    'Cookie: session_id={}'.format(request.json()['sessionId']),
                    'Cache-Control: no-cache',
                    'Pragma: no-cache',
                ]
                telemetry_ws = 'wss://{}/aeris/v1/wrpc/'.format(cmd_args.telemetryUrl)
                self.socket = websocket.WebSocketApp(
                    telemetry_ws,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                    header=headers,
                )
            else:
                logging.error('Telemetry credentials invalid. Could not log in.')
                exit()

        if cmd_args.noEmailSSL:
            self.server = smtplib.SMTP(cmd_args.emailServer, cmd_args.port)
        else:
            self.server = smtplib.SMTP_SSL(cmd_args.emailServer, cmd_args.port)
        try:
            self.server.login(cmd_args.userName, passwords['emailPassword'])
        except Exception as e:
            print e
            exit()

        self.config = cmd_args
        self.devices = {}
        self.devices_get_token = None
        self.devices_sub_token = None
        self.events_token = None
        self.socket.on_open = self.on_run

    def on_run(self, _):
        """
        Methods to run when the ws connects
        """
        logging.info('Websocket connected.')
        self.get_and_subscribe_devices()
        self.get_events()

    def send_message(self, command, token, args, version='0.9.0'):
        """
        Formats a message to be send to Telemetry WS server
        """
        arg_name = 'args' if version == '0.9.0' else 'params'
        data = {
            'token': token,
            'command': command,
            arg_name: args,
            'version': version,
        }
        self.socket.send(json.dumps(data))

    @staticmethod
    def on_close(_):
        """
        Run when ws closes.
        """
        logging.info('Websocket connection closed.')

    @staticmethod
    def on_error(_, error):
        """
        Print websocket error
        """
        if type(error) is KeyboardInterrupt:
            return

        logging.error('Websocket connection error: {}'.format(error))

    @staticmethod
    def make_token():
        """
        Generate request token
        """
        seed = ''.join(random.choice(string.ascii_uppercase + string.digits)
                       for _ in range(20))
        token = SHA256.new(seed).hexdigest()[0:38]
        return token

    def on_message(self, _, message):
        """
        Print message received from websocket
        """
        data = json.loads(message)
        if data['token'] == self.events_token:
            if 'result' in data:
                for event in data['result'][0]['Notifications']:
                    self.send_email(event)
        elif (data['token'] == self.devices_get_token or
              data['token'] == self.devices_sub_token):
            if 'result' in data:
                switch_updates = data['result'][0]['Notifications']
                self.process_devices(switch_updates[len(switch_updates) - 1])

    def get_events(self):
        """
        Subscribes to Telemetry events
        """
        logging.info('Subscribing to Telemetry events')
        self.events_token = self.make_token()
        args = {'query': {'analytics': {'/events/v1/allEvents': True}}}
        subscribe = threading.Thread(
            target=self.send_message,
            args=(SUBSCRIBE, self.events_token, args, VERSION_1)
        )
        subscribe.start()

    def get_and_subscribe_devices(self):
        """
        Subscribes to the list of devices that are streaming data to CVP.
        We'll use this list of devices keyed by the serial number to add more
        info to the email.
        """
        logging.info('Subscribing to Telemetry devices.')
        self.devices_get_token = self.make_token()
        self.devices_sub_token = self.make_token()

        # Get the current object
        get_args = {
            'query': {'analytics': {'/DatasetInfo/EosSwitches': True}},
            'count': False,
        }
        get_devices = threading.Thread(
            target=self.send_message,
            args=(GET, self.devices_get_token, get_args, VERSION_1),
        )
        get_devices.start()

        # subscribe for future changes
        args = {'query': {'analytics': {'/DatasetInfo/EosSwitches': True}}}
        subscribe = threading.Thread(
            target=self.send_message,
            args=(SUBSCRIBE, self.devices_sub_token, args, VERSION_1),
        )
        subscribe.start()

    def process_devices(self, devices):
        """
        Iterate through the list of devices and store the mapping of
        serial number to hostname
        """
        for key, value in devices['updates'].items():
            self.devices[key] = value['value']['hostname']

        logging.info('Received devices. Total device count is {}.'.format(len(self.devices)))

    def send_email(self, event):
        """
        Send an email using variables above
        """
        if 'data' not in event['updates']:
            return

        logging.debug('Preparing email notification.')

        # Gather data for message
        update = event['updates']
        data = update['data']['value']

        # Try to lookup the hostname, if not found return the serialnum
        host = self.devices.get(data.get('deviceId'), data.get('deviceId'))
        severity = update['severity']['value']
        title = update['title']['value']
        desc = update['description']['value']
        timestamp = update['timestamp']['value'] / 1000  # ms to sec
        datetime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))

        body = '''{} event on {} at {}\n \
        Description: {}\n \
        View Event at {}/telemetry/events\n'''.format(severity, host, datetime, desc, self.config.telemetryUrl)

        message = MIMEText(body)

        message['From'] = self.config.userName
        message['To'] = self.config.sendToAddress
        if self.config.sendCCAddress:
            message['Cc'] = self.config.sendCCAddress
        message['Subject'] = '{} {} {}'.format(self.config.subjectPrefix, severity, title)

        self.server.sendmail(self.config.userName,
                             self.config.sendToAddress.split(','),
                             message.as_string())
        logging.info('Email sent for event: {} {}'.format(severity, title))


def main():
    parser = argparse.ArgumentParser(description='Redirect streaming events as email notifications.')

    parser.add_argument(
        'telemetryUrl',
        help='IP address or hostname of CVP or Telemetry',
    )
    parser.add_argument(
        'emailServer',
        help='IP address or hostname of email server',
    )
    parser.add_argument(
        'userName',
        help='Email username, eg bob@acme.com',
    )
    parser.add_argument(
        'sendToAddress',
        help='Comma-separated list of email recipients',
    )
    parser.add_argument(
        '-c',
        '--sendCCAddress',
        help='Comma-separated list of email recipients',
    )
    parser.add_argument(
        '-s',
        '--subjectPrefix',
        default='[CloudVision Telemetry]',
        help='Text to prefix the Subject line',
    )
    parser.add_argument(
        '-p',
        '--port',
        type=int,
        default=465,
        help='destination port on SMTP server',
    )
    parser.add_argument(
        '--noEmailSSL',
        action='store_true',
        default=False,
        help='Flag to disable SSL SMTP connection',
    )
    parser.add_argument(
        '--noTelemetrySSL',
        action='store_true',
        default=False,
        help='Flag to disable SSL websocket connection',
    )
    parser.add_argument(
        '--telemetryUsername',
        help='Telemetry username if authentication is required',
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        default=False,
        help='Display additional info messages'
    )

    cmd_args = parser.parse_args()
    passwords = dict()

    try:
        passwords['emailPassword'] = getpass.getpass('Enter password for {}'.format(cmd_args.userName))
        if not cmd_args.noTelemetrySSL:
            passwords['telemetryPassword'] = getpass.getpass('Enter password for {}'.format(cmd_args.telemetryUrl))
    except KeyboardInterrupt:
        exit()

    logging_level = logging.DEBUG if cmd_args.verbose else logging.WARNING
    logging.basicConfig(level=logging_level)

    connection = TelemetryWs(cmd_args, passwords)

    try:
        connection.socket.run_forever(sslopt={
            'check_hostname': False,
            'cert_reqs': ssl.CERT_NONE
        })
    except KeyboardInterrupt:
        connection.socket.close()
        exit()


if __name__ == '__main__':
    main()
