#!/usr/bin/python3
import os.path
import configparser
import lxml.etree
import jstyleson
from collections import defaultdict
from datetime import datetime
import requests
from jinja2 import Environment, FileSystemLoader
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import re

rootdir = os.path.dirname(os.path.realpath(__file__))
config_file = os.path.join(rootdir, 'configuration.ini')

config = configparser.ConfigParser(
    interpolation=configparser.BasicInterpolation())
config.read(config_file)

devices_ignored = jstyleson.loads(config['DOMOTICZ']['DEVICES_IGNORED'])
devices_timeout_period = jstyleson.loads(
    config['DOMOTICZ']['DEVICES_TIMEOUT_PERIOD'])


def get_devices():
    response = requests.get(config['DOMOTICZ']['API_ALL_DEVICES'])
    devices = response.json()['result']

    return devices


def get_health_checks():
    response = requests.get(config['HEALTH_CHECKS']['API_ALL_CHECKS'],  headers={
                            "X-Api-Key": config['HEALTH_CHECKS']['X-API-KEY']})
    checks = [check for check in response.json()['checks']
              if check['status'] != 'up']

    return checks


def get_monit_services():
    response = requests.get(config['MONIT']['API_STATUS'])
    doc = lxml.etree.XML(response.content)

    services = [{'status': '??', 'name': service.find('name').text}
                for service in doc.iterfind('service/[@type="3"]')
                if service.find('monitor').text == '1'
                and service.find('status').text != '0']

    return services


def get_log_errors():
    log_messages_ignored = jstyleson.loads(config['DOMOTICZ']['LOG_MESSAGES_IGNORED'])
    response = requests.get(config['DOMOTICZ']['API_LOG_ERRORS'])
    items = response.json()['result']

    errors = defaultdict(int)
    for item in items:
        ignore_log_message = False
        key = item['message'][32:]
        for message in log_messages_ignored:
            if (re.search(message, key)):
                ignore_log_message = True
                break

        if (not ignore_log_message):
            errors[key] += 1

    return [{'text': key, 'count': errors[key]} for key in errors.keys()]


def get_devices_inactive(devices):

    now = datetime.now()
    devices_inactive = []

    for device in devices:

        device_idx = device['idx']

        if device_idx in devices_ignored:
            continue

        last_update = datetime.strptime(
            device['LastUpdate'], '%Y-%m-%d %H:%M:%S')

        timeout_period = config.getint('DEFAULT', 'TIMEOUT_PERIOD')

        if device_idx in devices_timeout_period:
            timeout_period = devices_timeout_period[device_idx]

        if ((now - last_update).days >= timeout_period):
            devices_inactive.append(device)

    return devices_inactive


def get_zway_devices_failed():
    '''returns a list of devices which have metrics.isFailed == true'''
    devices_inactive = []

    response = requests.get(config['ZWAY']['API_ALL_DEVICES'],  headers={
                            "ZWaySession": config['ZWAY']['ZWAYSESSION']})
    devices = response.json()['data']['devices']
    for device in devices:
        if 'metrics' in device and 'isFailed' in device['metrics'] and device['metrics']['isFailed'] == True:
            devices_inactive.append(
                {
                    'id': device['nodeId'],
                    'name': device['metrics']['title'],
                    'location': device['locationName'],
                })

    return devices_inactive


def get_devices_no_roomplan(devices):
    return [d for d in devices if d['PlanID'] == '0']


def send_mail(report):

    msg = MIMEMultipart('alternative')
    msg['Subject'] = config['MAIL']['SUBJECT']
    msg['From'] = config['MAIL']['FROM']
    msg['To'] = config['MAIL']['TO']
    msg.attach(MIMEText(report, 'html'))

    context = ssl.create_default_context()
    server = smtplib.SMTP(config['MAIL']['SERVER'],
                          config.getint('MAIL', 'PORT'))
    server.ehlo()
    server.starttls(context=context)
    server.ehlo()
    server.login(config['MAIL']['FROM'], config['MAIL']['PASSWORD'])
    server.sendmail(config['MAIL']['FROM'],
                    config['MAIL']['TO'], msg.as_string())
    server.quit()


zway_devices = get_zway_devices_failed()

monit_services = get_monit_services()

health_checks = get_health_checks()

devices = get_devices()

devices_inactive = get_devices_inactive(devices)
devices_no_roomplan = get_devices_no_roomplan(devices)
log_errors = get_log_errors()

context = {
    'devices_inactive':
        sorted(devices_inactive, key=lambda d: d['LastUpdate']),
    'devices_no_roomplan':
        sorted(devices_no_roomplan, key=lambda d: d['idx']),
    'log_errors':
        sorted(log_errors, key=lambda e: e['count'], reverse=True),
    'health_checks':
        health_checks,
    'monit_services':
        monit_services,
    'zway_devices':
        zway_devices
}

file_loader = FileSystemLoader('templates')
env = Environment(loader=file_loader, line_statement_prefix='#')

template = env.get_template('mail.html.j2')
report = template.render(context)

with open('rendered.html', 'w') as file:
    file.write(report)

if config.getboolean('MAIL', 'SEND_MAIL') == True:
    send_mail(report)
