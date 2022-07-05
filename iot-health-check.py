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

domoticz_devices_ignored = jstyleson.loads(config['DOMOTICZ']['DEVICES_IGNORED'])
domoticz_devices_timeout_period = jstyleson.loads(
    config['DOMOTICZ']['DEVICES_TIMEOUT_PERIOD'])
domoticz_devices_low_battery = jstyleson.loads(config['DOMOTICZ']['DEVICES_LOW_BATTERY'])

def get_domoticz_devices():
    '''returns list of domoticz devices'''
    response = requests.get(config['DOMOTICZ']['API_ALL_DEVICES'])
    devices = response.json()['result']

    return devices


def get_health_checks():
    '''returns list of health checks with status != up'''
    response = requests.get(config['HEALTH_CHECKS']['API_ALL_CHECKS'],  headers={
                            "X-Api-Key": config['HEALTH_CHECKS']['X-API-KEY']})
    checks = [check for check in response.json()['checks']
              if check['status'] != 'up']

    return checks


def get_monit_services():
    '''returns list of monit processes with status != OK'''
    response = requests.get(config['MONIT']['API_STATUS'])
    doc = lxml.etree.XML(response.content)

    services = [{'status': '??', 'name': service.find('name').text}
                for service in doc.iterfind('service/[@type="3"]')
                if service.find('monitor').text == '1'
                and service.find('status').text != '0']

    return services


def get_log_errors():
    '''returns log errors (unduplicated)'''
    log_messages_ignored = jstyleson.loads(config['DOMOTICZ']['LOG_MESSAGES_IGNORED'])
    response = requests.get(config['DOMOTICZ']['API_LOG_ERRORS'])

    if not 'result' in response.json():
        return []

    items = response.json()['result']

    errors = defaultdict(int)
    for item in items:
        ignore_log_message = False
        key = item['message'][32:]
        for message in log_messages_ignored:
            if re.search(message, key):
                ignore_log_message = True
                break

        if not ignore_log_message:
            errors[key] += 1

    return [{'text': key, 'count': errors[key]} for key in errors.keys()]


def get_devices_inactive(domoticz_devices):
    '''returns list of devices which that where inactive for certain period'''
    now = datetime.now()
    devices_inactive = []

    for device in domoticz_devices:

        device_idx = device['idx']

        if device_idx in domoticz_devices_ignored:
            continue

        last_update = datetime.strptime(
            device['LastUpdate'], '%Y-%m-%d %H:%M:%S')

        timeout_period = config.getint('DEFAULT', 'TIMEOUT_PERIOD')

        if device_idx in domoticz_devices_timeout_period:
            timeout_period = domoticz_devices_timeout_period[device_idx]

        if (now - last_update).days >= timeout_period:
            devices_inactive.append(device)

    return devices_inactive


def get_devices_low_battery(domoticz_devices):
    '''returns list of devices with low battery'''
    devices_low_battery = []
    low_battery_level = int(config['DEFAULT']['LOW_BATTERY_LEVEL'])

    for device in domoticz_devices:

        device_idx = device['idx']

        if not device_idx in domoticz_devices_low_battery:
            continue

        if int(device['BatteryLevel']) < low_battery_level:
            devices_low_battery.append(device)

    return devices_low_battery


def get_zway_devices():
    '''returns list of zway devices'''
 
    response = requests.get(config['ZWAY']['API_ALL_DEVICES'],  headers={
                            "ZWaySession": config['ZWAY']['ZWAYSESSION']})
    zway_devices = response.json()['data']['devices']

    return [d for d in zway_devices if d['technology'] == 'Z-Wave']


def get_zway_devices_failed(zway_devices):
    '''returns list of devices which have metrics.isFailed == true'''

    return [d for d in zway_devices if d['metrics']['isFailed']]

def get_zway_devices_low_battery(zway_devices):
    '''returns list of devices which have deviceType == 'battery' and metrics.level == true'''

    low_battery_level = int(config['DEFAULT']['LOW_BATTERY_LEVEL'])

    return [d for d in zway_devices if d['deviceType'] == 'battery' and d['metrics']['level'] < low_battery_level]

def get_devices_no_roomplan(devices):
    '''returns list of devices which are not part of a roomplan'''
    return [d for d in devices if d['PlanID'] == '0']


def send_mail(report):
    '''sends report via email'''
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

monit_services = get_monit_services()

health_checks = get_health_checks()

domoticz_devices = get_domoticz_devices()

devices_inactive = get_devices_inactive(domoticz_devices)
devices_low_battery = get_devices_low_battery(domoticz_devices)
devices_no_roomplan = get_devices_no_roomplan(domoticz_devices)
log_errors = get_log_errors()

zway_devices = get_zway_devices()
zway_devices_failed = get_zway_devices_failed(zway_devices)
zway_devices_low_battery = get_zway_devices_low_battery(zway_devices)

context = {
    'devices_inactive':
        sorted(devices_inactive, key=lambda d: d['LastUpdate']),
    'devices_low_battery':
        devices_low_battery,
    'devices_no_roomplan':
        sorted(devices_no_roomplan, key=lambda d: d['idx']),
    'log_errors':
        sorted(log_errors, key=lambda e: e['count'], reverse=True),
    'health_checks':
        health_checks,
    'monit_services':
        monit_services,
    'zway_devices_failed':
        zway_devices_failed,
    'zway_devices_low_battery':
        zway_devices_low_battery
}

file_loader = FileSystemLoader('templates')
env = Environment(loader=file_loader, line_statement_prefix='#')

template = env.get_template('mail.html.j2')
report = template.render(context)

with open('rendered.html', 'w') as file:
    file.write(report)

if config.getboolean('MAIL', 'SEND_MAIL') == True:
    send_mail(report)
