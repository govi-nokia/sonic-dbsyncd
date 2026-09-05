import os
import sys
# noinspection PyUnresolvedReferences
import tests.mock_tables.dbconnector
import time


modules_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(modules_path, 'src'))

from unittest import TestCase
import json
import mock
import re
import lldp_syncd
import lldp_syncd.conventions
import lldp_syncd.daemon
from swsscommon.swsscommon import SonicV2Connector

INPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'subproc_outputs')
TABLE_PREFIX = "LLDP_ENTRY_TABLE:"

def create_dbconnector():
    db = SonicV2Connector()
    db.connect(db.APPL_DB)
    return db


def make_seconds(days, hours, minutes, seconds):
    """
    >>> make_seconds(0,5,9,5)
    18545
    """
    return seconds + (60 * minutes) + (60 * 60 * hours) + (24 * 60 * 60 * days)


class TestLldpSyncDaemon(TestCase):
    def setUp(self):
        with open(os.path.join(INPUT_DIR, 'lldpctl.json')) as f:
            self._json = json.load(f)

        with open(os.path.join(INPUT_DIR, 'lldpctl_mgmt_only.json')) as f:
            self._json_short = json.load(f)

        with open(os.path.join(INPUT_DIR, 'short_short.json')) as f:
            self._json_short_short = json.load(f)

        with open(os.path.join(INPUT_DIR, 'lldpctl_single_loc_mgmt_ip.json')) as f:
            self._single_loc_mgmt_ip = json.load(f)

        with open(os.path.join(INPUT_DIR, 'interface_only.json')) as f:
            self._interface_only = json.load(f)

        with open(os.path.join(INPUT_DIR, 'lldpctl_no_neighbors_loc_mgmt_ip.json')) as f:
            self._no_neighbors_loc_mgmt_ip = json.load(f)

        with open(os.path.join(INPUT_DIR, 'lldpcli_statistics.json')) as f:
            self._statistics = json.load(f)

        self.daemon = lldp_syncd.LldpSyncDaemon()

    def test_parse_json(self):
        jo = self.daemon.parse_update(self._json)
        print(json.dumps(jo, indent=3))

    def test_parse_short(self):
        jo = self.daemon.parse_update(self._json_short)
        print(json.dumps(jo, indent=3))

    def test_parse_short_short(self):
        jo = self.daemon.parse_update(self._json_short_short)
        print(json.dumps(jo, indent=3))

    def test_sync_roundtrip(self):
        parsed_update = self.daemon.parse_update(self._json)
        self.daemon.sync(parsed_update)
        db = create_dbconnector()
        keys = db.keys(db.APPL_DB)

        dump = {}
        for k in keys:
            # The test case is for LLDP neighbor information.
            # Need to filter LLDP_LOC_CHASSIS entry because the entry is removed from parsed_update after executing daemon.sync().
            if k != 'LLDP_LOC_CHASSIS':
                dump[k] = db.get_all(db.APPL_DB, k)
        print(json.dumps(dump, indent=3))

        # convert dict keys to ints for easy comparison
        jo = {'LLDP_ENTRY_TABLE:'+ k: v for k, v in parsed_update.items()}
        self.assertEqual(jo, dump)

        # test enumerations
        for k, v in dump.items():
            chassis_subtype = v['lldp_rem_chassis_id_subtype']
            chassis_id = v['lldp_rem_chassis_id']
            if int(chassis_subtype) == lldp_syncd.conventions.LldpChassisIdSubtype.macAddress:
                if re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', chassis_id) is None:
                    self.fail("Non-mac returned for chassis ID")
            else:
                self.fail("Test data only contains chassis MACs")

    def test_timeparse(self):
        self.assertEqual(lldp_syncd.daemon.parse_time("0 day, 05:09:02"), make_seconds(0, 5, 9, 2))
        self.assertEqual(lldp_syncd.daemon.parse_time("2 days, 05:59:02"), make_seconds(2, 5, 59, 2))
        self.assertEqual(lldp_syncd.daemon.parse_time("-2 days, -23:-55:-02"), make_seconds(0, 0, 0, 0))

    def parse_mgmt_ip(self, json_file):
        parsed_update = self.daemon.parse_update(json_file)
        mgmt_ip_str = parsed_update['local-chassis'].get('lldp_loc_man_addr')
        json_chassis = json.dumps(json_file['lldp_loc_chassis']['local-chassis']['chassis'])
        chassis_dict = json.loads(json_chassis)
        json_mgmt_ip = list(chassis_dict.values())[0]['mgmt-ip']
        if isinstance(json_mgmt_ip, list):
            i=0
            for mgmt_ip in mgmt_ip_str.split(','):
                self.assertEqual(mgmt_ip, json_mgmt_ip[i])
                i+=1
        else:
            self.assertEqual(mgmt_ip_str, json_mgmt_ip)

    def test_multiple_mgmt_ip(self):
        self.parse_mgmt_ip(self._json)

    def test_single_mgmt_ip(self):
        self.parse_mgmt_ip(self._single_loc_mgmt_ip)

    def test_local_mgmt_ip_no_neighbors(self):
        self.parse_mgmt_ip(self._no_neighbors_loc_mgmt_ip)

    def test_loc_chassis(self):
        parsed_update = self.daemon.parse_update(self._json)
        parsed_loc_chassis = parsed_update['local-chassis']
        self.daemon.sync(parsed_update)
        db = create_dbconnector()
        db_loc_chassis_data = db.get_all(db.APPL_DB, 'LLDP_LOC_CHASSIS')
        self.assertEqual(parsed_loc_chassis, db_loc_chassis_data)

    def test_parse_sys_capabilities_lldpd_aliases_and_16bit(self):
        caps = [
            {"type": "Bridge", "enabled": True},
            {"type": "Router", "enabled": True},
            {"type": "Wlan", "enabled": False},
            {"type": "Station", "enabled": False},
            {"type": "C-VLAN", "enabled": True},
        ]
        # bits 2+4 -> 0x2800; Wlan bit 3 and Station bit 7 in supported
        self.assertEqual(
            self.daemon.parse_sys_capabilities(caps, enabled=True),
            "28 80",
        )
        self.assertEqual(
            self.daemon.parse_sys_capabilities(caps, enabled=False),
            "39 80",
        )
        interface_list = self._interface_only['lldp'].get('interface')
        for interface in interface_list:
            (if_name, if_attributes), = interface.items()
            capability_list = self.daemon.get_sys_capability_list(if_attributes, if_name, "fake_chassis_id")
            self.assertNotEqual(capability_list, [])

    def test_changed_deleted_interface(self):
        parsed_update = self.daemon.parse_update(self._json)
        self.daemon.sync(parsed_update)
        db = create_dbconnector()
        keys = db.keys(db.APPL_DB)
        # Check if each lldp_rem_time_mark is changed
        dump = {}
        for k in keys:
            if k != 'LLDP_LOC_CHASSIS':
                if 'eth0' in k or 'Ethernet0' in k:
                    dump[k] = db.get(db.APPL_DB, k, 'lldp_rem_time_mark')
                elif 'Ethernet100' in k:
                    dump[k] = db.get(db.APPL_DB, k, 'lldp_rem_port_desc')

        time.sleep(1)
        # simulate lldp_rem_time_mark was changed or port description was changed or interface was removed
        changed_json = self._json.copy()
        changed_json['lldp']['interface'][0]['eth0']['age'] = '0 day, 05:09:12'
        changed_json['lldp']['interface'][1]['Ethernet0']['age'] = '0 day, 05:09:15'
        changed_json['lldp']['interface'][2]['Ethernet100']['port']['descr'] = "I'm a little teapot, too."
        changed_json['lldp']['interface'].pop(3) # Remove interface Ethernet104

        parsed_update = self.daemon.parse_update(changed_json)
        self.daemon.sync(parsed_update)
        keys = db.keys(db.APPL_DB)

        jo = {}
        for k in keys:
            if k != 'LLDP_LOC_CHASSIS':
                if 'eth0' in k or 'Ethernet0' in k:
                    jo[k] = db.get(db.APPL_DB, k, 'lldp_rem_time_mark')
                    self.assertEqual(int(jo[k]), int(dump[k])+10)
                elif 'Ethernet100' in k:
                    jo[k] = db.get(db.APPL_DB, k, 'lldp_rem_port_desc')
                    self.assertEqual(dump[k], "")
                    self.assertEqual(jo[k], "I'm a little teapot, too.")
                else:
                    jo[k] = db.get_all(db.APPL_DB, k)
        if 'LLDP_ENTRY_TABLE:Ethernet104' in jo:
            self.fail("After removing Ethernet104, it is still found in APPL_DB!")

    @mock.patch('subprocess.check_output')
    def test_invalid_chassis_name(self, mock_check_output):
        # mock the invalid chassis name
        mock_check_output.return_value = '''
        {
            "local-chassis": {
                "chassis": {
                    "chassis_name\1": {
                        "id": {
                        "type": "mac",
                        "value": "aa:bb:cc:dd:ee:ff"
                        },
                        "descr": "SONiC Software Version: SONiC.20230531.22",
                        "capability": [
                            {
                                "type": "Bridge",
                                "enabled": true
                            },
                            {
                                "type": "Router",
                                "enabled": true
                            },
                            {
                                "type": "Wlan",
                                "enabled": false
                            },
                            {
                                "type": "Station",
                                "enabled": false
                            }
                        ]
                    }
                }
            }
        }
        '''
        result = self.daemon.source_update()
        self.assertIsNone(result)


    def test_changed_interface(self):
        parsed_update = self.daemon.parse_update(self._json)
        self.daemon.sync(parsed_update)
        db = create_dbconnector()
        keys = db.keys(db.APPL_DB)
        # Check if each lldp_rem_time_mark is changed
        dump = {}
        for k in keys:
            if k != 'LLDP_LOC_CHASSIS':
                if TABLE_PREFIX + 'eth0' == k or TABLE_PREFIX + 'Ethernet0' == k:
                    dump[k] = db.get(db.APPL_DB, k, 'lldp_rem_time_mark')

        time.sleep(1)
        # simulate lldp_rem_time_mark was changed or port description was changed or interface was removed
        changed_json = self._json.copy()
        changed_json['lldp']['interface'][0]['eth0']['age'] = '0 day, 05:09:12'
        changed_json['lldp']['interface'][1]['Ethernet0']['age'] = '0 day, 05:09:15'

        parsed_update = self.daemon.parse_update(changed_json)
        self.daemon.sync(parsed_update)
        keys = db.keys(db.APPL_DB)

        jo = {}
        for k in keys:
            if k != 'LLDP_LOC_CHASSIS':
                if TABLE_PREFIX + 'eth0' == k or TABLE_PREFIX + 'Ethernet0' == k:
                    jo[k] = db.get(db.APPL_DB, k, 'lldp_rem_time_mark')
                    self.assertEqual(int(jo[k]), int(dump[k])+10)
                else:
                    jo[k] = db.get_all(db.APPL_DB, k)
        time.sleep(1)
        # simulate lldp_rem_time_mark was changed or port description was changed or interface was removed
        changed_json = self._json.copy()
        changed_json['lldp']['interface'][0]['eth0']['age'] = '0 day, 05:09:12'
        changed_json['lldp']['interface'][1]['Ethernet0']['port']['descr'] = 'Ethernet1'

        parsed_update = self.daemon.parse_update(changed_json)
        self.daemon.sync(parsed_update)
        keys = db.keys(db.APPL_DB)

        jo = {}
        for k in keys:
            if k != 'LLDP_LOC_CHASSIS':
                if TABLE_PREFIX + 'eth0' == k:
                    jo[k] = db.get(db.APPL_DB, k, 'lldp_rem_time_mark')
                    self.assertEqual(int(jo[k]), int(dump[k])+10)
                elif TABLE_PREFIX + 'Ethernet0' == k:
                    jo[k] = db.get(db.APPL_DB, k, 'lldp_rem_port_desc')
                    self.assertEqual(jo[k], 'Ethernet1')
                else:
                    jo[k] = db.get_all(db.APPL_DB, k)

    def test_parse_openconfig_1_2_0_state_fields(self):
        parsed = self.daemon.parse_update(self._json)

        eth0 = parsed['eth0']
        self.assertEqual(eth0['lldp_rem_ttl'], '90')
        self.assertEqual(eth0['lldp_rem_max_frame_size'], '1514')
        self.assertEqual(eth0['lldp_rem_port_vlan_id'], '146')
        self.assertEqual(eth0['lldp_rem_agg_port_id'], '')
        self.assertEqual(eth0['lldp_rem_med_inv_serial'], '')
        custom = json.loads(eth0['lldp_rem_custom_tlvs'])
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0]['type'], 127)
        self.assertEqual(custom[0]['oui'], '00:90:69')
        self.assertEqual(custom[0]['oui-subtype'], '1')
        self.assertEqual(custom[0]['value'], '435530323133353130363530')

        ethernet0 = parsed['Ethernet0']
        self.assertEqual(ethernet0['lldp_rem_ttl'], '120')
        self.assertEqual(ethernet0['lldp_rem_max_frame_size'], '9236')
        self.assertEqual(ethernet0['lldp_rem_port_vlan_id'], '101')
        self.assertEqual(ethernet0['lldp_rem_custom_tlvs'], '')

        loc = parsed['local-chassis']
        self.assertEqual(loc['lldp_loc_ttl'], '120')
        self.assertEqual(loc['lldp_loc_man_addr'], '10.1.0.32,fc00:1::32')

    def test_parse_aggregation_and_med_serial(self):
        sample = {
            'lldp': {
                'interface': [{
                    'Ethernet0': {
                        'rid': '1',
                        'age': '0 day, 00:00:10',
                        'port': {
                            'id': {'type': 'ifname', 'value': 'Ethernet1'},
                            'descr': 'agg member',
                            'mfs': '9216',
                            'aggregation': '42',
                        },
                        'chassis': {
                            'peer': {
                                'id': {'type': 'mac', 'value': '00:11:22:33:44:55'},
                                'ttl': '120',
                                'descr': 'peer',
                            }
                        },
                        'lldp-med': {
                            'inventory': {
                                'serial': 'SN-1234',
                            }
                        },
                    }
                }]
            }
        }
        parsed = self.daemon.parse_update(sample)
        entry = parsed['Ethernet0']
        self.assertEqual(entry['lldp_rem_agg_port_id'], '42')
        self.assertEqual(entry['lldp_rem_med_inv_serial'], 'SN-1234')
        self.assertEqual(entry['lldp_rem_max_frame_size'], '9216')
        self.assertEqual(entry['lldp_rem_ttl'], '120')

    def test_chassis_cache_no_db_calls_when_unchanged(self):
        """
        Test that database operations are not called when chassis data hasn't changed.
        """
        # First sync to populate chassis_cache
        parsed_update = self.daemon.parse_update(self._json)
        self.daemon.sync(parsed_update)
        initial_cache = self.daemon.chassis_cache.copy()

        # Sync again with same data - DB operations should NOT be called
        parsed_update_same = self.daemon.parse_update(self._json)

        with mock.patch.object(self.daemon.db_connector, 'delete') as mock_delete, \
             mock.patch.object(self.daemon.db_connector, 'set') as mock_set:

            self.daemon.sync(parsed_update_same)

            # Verify chassis DB operations were NOT called
            chassis_deletes = [c for c in mock_delete.call_args_list
                             if len(c[0]) > 1 and c[0][1] == 'LLDP_LOC_CHASSIS']
            chassis_sets = [c for c in mock_set.call_args_list
                          if len(c[0]) > 1 and c[0][1] == 'LLDP_LOC_CHASSIS']

            self.assertEqual(len(chassis_deletes), 0)
            self.assertEqual(len(chassis_sets), 0)
            self.assertEqual(self.daemon.chassis_cache, initial_cache)

    def test_parse_statistics(self):
        parsed = self.daemon.parse_statistics(self._statistics)
        self.assertIsNotNone(parsed)
        self.assertNotIn('docker0', parsed)

        eth0 = parsed['eth0']
        self.assertEqual(eth0['frame_out'], '10')
        self.assertEqual(eth0['frame_in'], '20')
        self.assertEqual(eth0['frame_discard'], '1')
        self.assertEqual(eth0['tlv_unknown'], '2')
        self.assertEqual(eth0['entries_aged_out'], '3')
        self.assertEqual(eth0['tlv_accepted'], '4')

        ethernet0 = parsed['Ethernet0']
        self.assertEqual(ethernet0['frame_out'], '100')
        self.assertEqual(ethernet0['frame_in'], '200')
        self.assertEqual(ethernet0['frame_discard'], '6')
        self.assertEqual(ethernet0['tlv_unknown'], '7')
        self.assertEqual(ethernet0['entries_aged_out'], '8')
        self.assertEqual(ethernet0['tlv_accepted'], '9')

        global_stats = parsed['GLOBAL']
        self.assertEqual(global_stats['frame_out'], '110')
        self.assertEqual(global_stats['frame_in'], '220')
        self.assertEqual(global_stats['frame_discard'], '7')
        self.assertEqual(global_stats['tlv_unknown'], '9')
        self.assertEqual(global_stats['entries_aged_out'], '11')
        self.assertEqual(global_stats['tlv_accepted'], '13')

    def test_parse_statistics_summary_only(self):
        sample = {
            'lldp': {
                'summary': {
                    'tx': '15',
                    'rx': '25',
                    'rx_discarded_cnt': '0',
                    'rx_unrecognized_cnt': '1',
                    'ageout_cnt': '2',
                    'insert_cnt': '3',
                }
            }
        }
        parsed = self.daemon.parse_statistics(sample)
        self.assertEqual(parsed['GLOBAL']['frame_out'], '15')
        self.assertEqual(parsed['GLOBAL']['frame_in'], '25')
        self.assertEqual(parsed['GLOBAL']['tlv_unknown'], '1')
        self.assertEqual(parsed['GLOBAL']['tlv_accepted'], '3')
        self.assertEqual(parsed['GLOBAL']['entries_aged_out'], '2')
        self.assertEqual(list(parsed.keys()), ['GLOBAL'])

    def test_parse_update_does_not_mix_statistics_into_neighbors(self):
        payload = json.loads(json.dumps(self._json))
        payload['lldp_statistics'] = self._statistics
        parsed = self.daemon.parse_update(payload)
        self.assertNotIn('GLOBAL', parsed)
        self.assertNotIn('frame_in', parsed['Ethernet0'])
        self.assertIsNotNone(self.daemon._pending_statistics)
        self.assertIn('GLOBAL', self.daemon._pending_statistics)

    def test_sync_statistics_roundtrip(self):
        payload = json.loads(json.dumps(self._json))
        payload['lldp_statistics'] = self._statistics
        parsed_update = self.daemon.parse_update(payload)
        self.daemon.sync(parsed_update)

        db = self.daemon.db_connector
        eth0 = db.get_all(db.COUNTERS_DB, 'LLDP_STATISTICS:eth0')
        ethernet0 = db.get_all(db.COUNTERS_DB, 'LLDP_STATISTICS:Ethernet0')
        global_stats = db.get_all(db.COUNTERS_DB, 'LLDP_STATISTICS:GLOBAL')

        self.assertEqual(eth0['frame_in'], '20')
        self.assertEqual(ethernet0['frame_out'], '100')
        self.assertEqual(global_stats['frame_in'], '220')
        self.assertFalse(db.exists(db.COUNTERS_DB, 'LLDP_STATISTICS:docker0'))

        # Neighbor APPL_DB keys are unchanged by the counters write.
        appl_keys = db.keys(db.APPL_DB)
        self.assertTrue(any(k.startswith(TABLE_PREFIX) for k in appl_keys))
        self.assertFalse(any(k.startswith('LLDP_STATISTICS:') for k in appl_keys))

    def test_sync_statistics_deletes_stale_interface(self):
        first = self.daemon.parse_statistics(self._statistics)
        self.daemon.sync_statistics(first)
        db = self.daemon.db_connector
        self.assertTrue(db.exists(db.COUNTERS_DB, 'LLDP_STATISTICS:eth0'))

        second = {
            'Ethernet0': first['Ethernet0'],
            'GLOBAL': first['Ethernet0'],
        }
        self.daemon.sync_statistics(second)
        self.assertFalse(db.exists(db.COUNTERS_DB, 'LLDP_STATISTICS:eth0'))
        self.assertTrue(db.exists(db.COUNTERS_DB, 'LLDP_STATISTICS:Ethernet0'))
        self.assertTrue(db.exists(db.COUNTERS_DB, 'LLDP_STATISTICS:GLOBAL'))
