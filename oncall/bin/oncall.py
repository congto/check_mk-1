#!/usr/bin/env python
# -*- encoding: utf-8; py-indent-offset: 4 -*-

# (C) 2016 Heinlein Support GmbH
# Robert Sander <r.sander@heinlein-support.de>

# in ~/etc

oncall_filename = 'oncall.csv'

# Format is:
# start;end;notify_plugin;adminonduty
# notify_plugin and adminonduty columns may be lists
# lists are separated by comma
# notify_plugin may be 'off duty' (see below default_offduty)
# to set an entry explicitely to off duty
# adminonduty can contain user or group names

oncall_group = u'oncall'
default_desc = u'oncall'
default_offduty = 'off duty'

import os
import csv
import time
from pprint import pprint, pformat
import argparse
import requests
import json
import warnings
import cmk.paths
import cmk.store

omdsite = os.environ.get('OMD_SITE')
now = time.gmtime() # dates and times are in UTC
timeformat = '%Y-%m-%dT%H:%M'
default_rule = {'comment': u'When this rule is disabled, the user is on-call.',
                'description': default_desc,
                'docu_url': '',
                'disabled': False,
                }
onduty = {}
offduty = []
notify_plugins = set()

notification_rules_filename = os.path.join(cmk.paths.omd_root, 'etc/check_mk/conf.d/wato/notifications.mk')
contacts_filename = os.path.join(cmk.paths.omd_root, 'etc/check_mk/conf.d/wato/contacts.mk')

omdconfig = {}
execfile(os.path.join(cmk.paths.omd_root, 'etc/omd/site.conf'), omdconfig, omdconfig)

parser = argparse.ArgumentParser()
parser.add_argument('-u', '--username', required=True, help='name of the Check_MK automation user')
parser.add_argument('-s', '--secret', required=True)
parser.add_argument('-d', '--debug', action='store_true', default=False, required=False)
args = parser.parse_args()

api_url = 'http://%s:%s/%s/check_mk/webapi.py' % (omdconfig['CONFIG_APACHE_TCP_ADDR'], omdconfig['CONFIG_APACHE_TCP_PORT'], omdsite)
api_creds = {'_username': args.username, '_secret': args.secret, 'output_format': 'json'}
api_activate = { 'action': 'activate_changes'}
api_activate.update(api_creds)

if args.debug:
    print api_url
    print api_activate

#
# Read list of noticication plugins
# where recipients are limited to the oncall_group
#
for rule in cmk.store.load_from_mk_file(notification_rules_filename, "notification_rules", []):
    if rule['allow_disable'] and 'contact_match_groups' in rule and oncall_group in rule['contact_match_groups']:
        notify_plugins.add(rule['notify_plugin'][0])

if args.debug:
    print "notify_plugins = %s" % pformat(notify_plugins)
        
#
# Read list of contacts
#
contacts = cmk.store.load_from_mk_file(contacts_filename, 'contacts', {}, lock=True)
contacts_old = contacts.copy()

#
# Determine who is on call right now
#
with open(os.path.join(cmk.paths.omd_root, 'etc', oncall_filename), 'rb') as oncallfile:
    csvreader = csv.DictReader(oncallfile, delimiter=';')
    for row in csvreader:
        try:
            start = time.strptime(row['start'], timeformat)
            end = time.strptime(row['end'], timeformat)
            if start <= now and now <= end:
                for part in row['adminonduty'].split(','):
                    if row['notify_plugin'] == default_offduty:
                        offduty.append(part)
                    else:
                        if part not in onduty:
                            onduty[part] = set()
                        onduty[part].update(row['notify_plugin'].split(','))
        except ValueError:
            print "unknown time format in %s" % row

if args.debug:
    print "onduty = %s" % pformat(onduty)
    print "contacts = %s" % pformat(contacts)
            
#
# Set oncall rules
#
for user, data in contacts.iteritems():
    if args.debug:
        print "user = %s" % user
    user_attr_oncall = ( 'contactgroups' in data and oncall_group in data['contactgroups'] )
    other_rules = []
    if 'notification_rules' in data:
        for rule in data['notification_rules']:
            if rule['description'] != default_desc:
                other_rules.append(rule)
    contacts[user]['notification_rules'] = []
    notify_plugins_onduty = []
    groupduty = False
    if 'contactgroups' in data:
        for contactgroup in data['contactgroups']:
            if ( contactgroup in onduty ) and ( contactgroup not in offduty ):
                groupduty = contactgroup

    if args.debug:
        print 'user_attr_oncall = %s' % pformat(user_attr_oncall)
        print 'offduty = %s' % pformat(offduty)
        print 'groupduty = %s' % pformat(groupduty)
    if user_attr_oncall:
        if ( user in onduty or groupduty ) and ( user not in offduty ):
            if user in onduty:
                notify_plugins_onduty = onduty[user]
            else:
                notify_plugins_onduty = onduty[groupduty]
            for notify_plugin in notify_plugins_onduty:
                rule = default_rule.copy()
                rule['contact_users'] = [ user ]
                rule['notify_plugin'] = (notify_plugin, None)
                rule['disabled'] = True
                contacts[user]['notification_rules'].append(rule)
                if args.debug:
                    print "%s is on duty for %s" % (user, notify_plugin)
        for notify_plugin in notify_plugins:
            if notify_plugin not in notify_plugins_onduty:
                rule = default_rule.copy()
                rule['contact_users'] = [ user ]
                rule['notify_plugin'] = (notify_plugin, None)
                contacts[user]['notification_rules'].append(rule)
                if args.debug:
                    print "%s is off duty for %s" % (user, notify_plugin)
        # write other rules at the end of the list
        contacts[user]['notification_rules'].extend(other_rules)
    elif args.debug:
        print "%s is not oncall" % user
    if args.debug:
        print

if args.debug:
    print "contacts = %s" % pformat(contacts)
    print "changes = %s" % (contacts != contacts_old)

if contacts == contacts_old and not args.debug:
    cmk.store.release_lock(contacts_filename)
else:
    def gen_id():
        try:
            return file('/proc/sys/kernel/random/uuid').read().strip()
        except IOError:
            # On platforms where the above file does not exist we try to
            # use the python uuid module which seems to be a good fallback
            # for those systems. Well, if got python < 2.5 you are lost for now.
            import uuid
            return str(uuid.uuid4())

    #
    # Write contacts
    #
    cmk.store.save_to_mk_file(contacts_filename, 'contacts', contacts)

    #
    # Set Replication State
    #
    import glob
    change_spec = {'id': gen_id(),
                   'action_name': 'edit-users',
                   'text': 'Modified user notification rules for oncall',
                   'object': None,
                   'user_id': args.username,
                   'domains': None,
                   'time': time.time(),
                   'need_sync': True,
                   'need_restart': False,
                  }
    sites = []
    if args.debug:
        pprint(change_spec)
    for changes_file in glob.iglob(os.path.join(cmk.paths.omd_root, "var/check_mk/wato/replication_changes_*.mk")):
        if args.debug:
            print changes_file
        try:
            cmk.store.aquire_lock(changes_file)
            with open(changes_file, 'a+') as f:
                f.write(repr(change_spec)+'\0')
                f.flush()
                os.fsync(f.fileno())
            os.chmod(changes_file, 0660)
        except Exception, e:
            raise 'Cannot write file "%s": %s' % (path, e)
        finally:
            cmk.store.release_lock(changes_file)
        sites.append(changes_file.split('/')[-1][20:-3])
        
    #
    # Activate Sites via WATO Web-API
    #
    def api_request(params, data=None, errmsg='Error', fail=True):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            if data:
                resp = requests.post(api_url, verify=False, params=params, data='request=%s' % json.dumps(data))
            else:
                resp = requests.get(api_url, verify=False, params=params)
            try:
                resp1 = resp.json()
            except:
                raise
            resp = resp1
        if resp['result_code'] == 1:
            if fail:
                raise RuntimeError('%s: %s' % ( errmsg, resp['result'] ))
            else:
                print '%s: %s' % ( errmsg, resp['result'] )
        return resp['result']

    for site in sites:
        if args.debug:
            print 'activating %s' % site
        api_request(params=api_activate, data={'sites': [site], 'allow_foreign_changes': '1'})
