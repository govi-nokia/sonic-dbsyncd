import datetime
import json
import re
import subprocess
import time
from collections import defaultdict

from enum import unique, Enum
from swsscommon.swsscommon import SonicV2Connector

from sonic_syncd import SonicSyncDaemon
from . import logger
from .conventions import (
    LldpPortIdSubtype,
    LldpChassisIdSubtype,
    LLDP_CAPABILITY_NAME_TO_BIT,
)

LLDPD_TIME_FORMAT = '%H:%M:%S'

DEFAULT_UPDATE_INTERVAL = 10

# IEEE 802.1AB Organizationally Specific TLV type. lldpd reports these as
# unknown-tlvs (OUI / subtype / value) without a type field.
LLDP_ORG_SPECIFIC_TLV_TYPE = 127

# Match Front | Backplace | Management interface
# TODO: Need to chamge to util function which can provide
# backplane interface name.
SONIC_ETHERNET_RE_PATTERN = r'^(Ethernet(\d+)|Ethernet-BP(\d+)|eth0)$'
LLDPD_UPTIME_RE_SPLIT_PATTERN = r' days?, '


def parse_time(time_str):
    """
    From LLDPd/src/client/display.c:
    static const char*
    display_age(time_t lastchange)
    {
        static char sage[30];
        int age = (int)(time(NULL) - lastchange);
        if (snprintf(sage, sizeof(sage),
            "%d day%s, %02d:%02d:%02d",
            age / (60*60*24),
            (age / (60*60*24) > 1)?"s":"",
            (age / (60*60)) % 24,
            (age / 60) % 60,
            age % 60) >= sizeof(sage))
            return "too much";
        else
            return sage;
    }
    :return: parsed age in time ticks (or seconds)
    """
    try:
        days, hour_min_secs = re.split(LLDPD_UPTIME_RE_SPLIT_PATTERN, time_str)
        struct_time = time.strptime(hour_min_secs, LLDPD_TIME_FORMAT)
        time_delta = datetime.timedelta(days=int(days), hours=struct_time.tm_hour,
                                        minutes=struct_time.tm_min,
                                        seconds=struct_time.tm_sec)
        return int(time_delta.total_seconds())
    except ValueError:
        logger.exception("Failed to parse lldp age {} -- ".format(time_str))
    return 0


def _as_list(value):
    """Normalize lldpd JSON that may be a dict (one item) or a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_oui(oui):
    """Convert lldpd OUI ('00,90,69' / '00:90:69' / '009069') to '00:90:69'."""
    if not oui:
        return ''
    cleaned = str(oui).replace(',', '').replace(':', '').replace('-', '').replace(' ', '')
    if len(cleaned) != 6:
        return str(oui)
    return ':'.join(cleaned[i:i + 2] for i in range(0, 6, 2)).lower()


def _hex_bytes_to_string(value):
    """Flatten lldpd comma/colon-separated hex bytes to a contiguous hex string."""
    if value is None:
        return ''
    if isinstance(value, list):
        return ''.join(str(part).strip() for part in value)
    return str(value).replace(',', '').replace(':', '').replace('-', '').replace(' ', '')


class LldpSyncDaemon(SonicSyncDaemon):
    """
    This script uploads lldp information to Redis DB.

    Neighbor and local chassis state go to APPL_DB. LLDP counters from
    `lldpcli show statistics` go to COUNTERS_DB (LLDP_STATISTICS).
    """
    LLDP_ENTRY_TABLE = 'LLDP_ENTRY_TABLE'
    LLDP_LOC_CHASSIS_TABLE = 'LLDP_LOC_CHASSIS'
    LLDP_STATISTICS_TABLE = 'LLDP_STATISTICS'
    LLDP_STATISTICS_GLOBAL_KEY = 'GLOBAL'

    # lldpd JSON key -> COUNTERS_DB / OpenConfig counter field
    LLDP_STAT_FIELD_MAP = (
        ('tx', 'frame_out'),
        ('rx', 'frame_in'),
        ('rx_discarded_cnt', 'frame_discard'),
        ('rx_unrecognized_cnt', 'tlv_unknown'),
        ('insert_cnt', 'tlv_accepted'),
        ('ageout_cnt', 'entries_aged_out'),
    )

    @unique
    class PortIdSubtypeMap(int, Enum):
        """
        This class follows the 802.1AB TEXTUAL-CONVENTION for mapping LLDP subtypes to integers (enum).
        `lldpd` does this as well.  This avoids using regex to parse `lldpd` data.

        From lldpd / src / lib / atoms / port.c:
        static lldpctl_map_t port_id_subtype_map[] = {
            { LLDP_PORTID_SUBTYPE_IFNAME,   "ifname"},
            { LLDP_PORTID_SUBTYPE_IFALIAS,  "ifalias" },
            { LLDP_PORTID_SUBTYPE_LOCAL,    "local" },
            { LLDP_PORTID_SUBTYPE_LLADDR,   "mac" },
            { LLDP_PORTID_SUBTYPE_ADDR,     "ip" },
            { LLDP_PORTID_SUBTYPE_PORT,     "unhandled" },
            { LLDP_PORTID_SUBTYPE_AGENTCID, "unhandled" },
            { 0, NULL},
        };
        """
        ifalias = int(LldpPortIdSubtype.interfaceAlias)
        # port =  LldpPortIdSubtype.portComponent # (unsupported by lldpd)
        mac = int(LldpPortIdSubtype.macAddress)
        ip = int(LldpPortIdSubtype.networkAddress)
        ifname = int(LldpPortIdSubtype.interfaceName)
        # agentcircuitid = int(LldpPortIdSubtype.agentCircuitId) # (unsupported by lldpd)
        local = int(LldpPortIdSubtype.local)

    @unique
    class ChassisIdSubtypeMap(int, Enum):
        """
        This class follows the 802.1AB TEXTUAL-CONVENTION for mapping LLDP subtypes to integers (enum).
        `lldpd` does this as well.  This avoids using regex to parse `lldpd` data.

        From lldpd / src / lib / atoms / chassis.c:
        static lldpctl_map_t chassis_id_subtype_map[] = {
            { LLDP_CHASSISID_SUBTYPE_IFNAME,  "ifname"},
            { LLDP_CHASSISID_SUBTYPE_IFALIAS, "ifalias" },
            { LLDP_CHASSISID_SUBTYPE_LOCAL,   "local" },
            { LLDP_CHASSISID_SUBTYPE_LLADDR,  "mac" },
            { LLDP_CHASSISID_SUBTYPE_ADDR,    "ip" },
            { LLDP_CHASSISID_SUBTYPE_PORT,    "unhandled" },
            { LLDP_CHASSISID_SUBTYPE_CHASSIS, "unhandled" },
            { 0, NULL},
        };
        """
        ifname = int(LldpChassisIdSubtype.interfaceName)
        ifalias = int(LldpChassisIdSubtype.interfaceAlias)
        # port =  int(LldpChassisIdSubtype.portComponent) # (unsupported by lldpd)
        mac = int(LldpChassisIdSubtype.macAddress)
        ip = int(LldpChassisIdSubtype.networkAddress)
        # chassis = int(LldpChassisIdSubtype.chassisComponent) # (unsupported by lldpd)
        local = int(LldpPortIdSubtype.local)

    def get_sys_capability_list(self, if_attributes, if_name, chassis_id):
        """
        Get a list of capabilities from interface attributes dictionary.
        :param if_attributes: interface attributes
        :return: list of capabilities
        """
        try:
            # [{'enabled': ..., 'type': 'capability1'}, {'enabled': ..., 'type': 'capability2'}]
            if 'capability' in if_attributes['chassis']:
                capability_list = if_attributes['chassis']['capability']
            else:
                capability_list = list(if_attributes['chassis'].values())[0]['capability']
            # {'enabled': ..., 'type': 'capability'}
            if isinstance(capability_list, dict):
                capability_list = [capability_list]
        except KeyError:
            logger.info("Failed to get system capabilities on {} ({})".format(if_name, chassis_id))
            return []
        return capability_list

    def parse_sys_capabilities(self, capability_list, enabled=False):
        """
        Get a bit map of capabilities, accoding to textual convention.
        :param capability_list: list of capabilities
        :param enabled: if true, consider only the enabled capabilities
        :return: string representing a bit map
        """
        # chassis is incomplete, missing capabilities
        if not capability_list:
            return ""

        sys_cap = 0
        for capability in capability_list:
            try:
                if (not enabled) or capability["enabled"]:
                    cap_name = capability["type"].lower().replace(" ", "")
                    bit = LLDP_CAPABILITY_NAME_TO_BIT[cap_name]
                    sys_cap |= 0x8000 >> bit
            except KeyError:
                logger.debug("Unknown capability {}".format(capability["type"]))
        return "%0.2X %0.2X" % ((sys_cap >> 8) & 0xFF, sys_cap & 0xFF)

    def __init__(self, update_interval=None):
        super(LldpSyncDaemon, self).__init__()
        self._update_interval = update_interval or DEFAULT_UPDATE_INTERVAL
        self.db_connector = SonicV2Connector()
        self.db_connector.connect(self.db_connector.APPL_DB)
        self.db_connector.connect(self.db_connector.COUNTERS_DB)

        self.chassis_cache = {}
        self.interfaces_cache = {}
        self.stats_cache = {}
        self._pending_statistics = None

    @staticmethod
    def _scrap_output(cmd):
        try:
            # execute the subprocess command
            lldpctl_output = subprocess.check_output(cmd)
        except subprocess.CalledProcessError:
            logger.exception("lldpctl exited with non-zero status")
            return None

        try:
            # parse the scrapped output
            lldpctl_json = json.loads(lldpctl_output)
        except ValueError:
            logger.exception("Failed to parse lldpctl output")
            return None

        return lldpctl_json

    def source_update(self):
        """
        Invoke lldpctl and format as JSON
        """
        cmd = ['/usr/sbin/lldpctl', '-f', 'json']
        logger.debug("Invoking lldpctl with: {}".format(cmd))
        cmd_local = ['/usr/sbin/lldpcli', '-f', 'json', 'show', 'chassis']
        logger.debug("Invoking lldpcli with: {}".format(cmd_local))
        cmd_stats = ['/usr/sbin/lldpcli', '-f', 'json', 'show', 'statistics']
        logger.debug("Invoking lldpcli with: {}".format(cmd_stats))

        lldp_json = self._scrap_output(cmd)
        if lldp_json is None:
            return None
        lldp_json['lldp_loc_chassis'] = self._scrap_output(cmd_local)
        # Statistics scrape is best-effort: neighbor sync still proceeds if
        # lldpcli show statistics fails or returns empty.
        stats_json = self._scrap_output(cmd_stats)
        if stats_json is not None:
            lldp_json['lldp_statistics'] = stats_json

        return lldp_json

    def parse_update(self, lldp_json):
        """
        Parse lldpd output to extract
        (1) LldpRemPortDesc;
        (2) LldpRemPortID;
        (3) LldpRemPortIdSubtype;
        (4) LldpRemSysName.

        LldpRemEntry ::= SEQUENCE {
              lldpRemTimeMark           TimeFilter,
              lldpRemLocalPortNum       LldpPortNumber,
              lldpRemIndex              Integer32,
              lldpRemChassisIdSubtype   LldpChassisIdSubtype,
              lldpRemChassisId          LldpChassisId,
              lldpRemPortIdSubtype      LldpPortIdSubtype,
              lldpRemPortId             LldpPortId,
              lldpRemPortDesc           SnmpAdminString,
              lldpRemSysName            SnmpAdminString,
              lldpRemSysDesc            SnmpAdminString,
              lldpRemSysCapSupported    LldpSystemCapabilitiesMap,
              lldpRemSysCapEnabled      LldpSystemCapabilitiesMap
        }
        """
        self._pending_statistics = None
        if isinstance(lldp_json, dict):
            self._pending_statistics = self.parse_statistics(lldp_json.get('lldp_statistics'))
        try:
            interface_list = lldp_json['lldp'].get('interface') or []
            parsed_interfaces = defaultdict(dict)
            for interface in interface_list:
                try:
                    # [{'if_name' : { attributes...}}, {'if_other': {...}}, ...]
                    (if_name, if_attributes), = interface.items()
                except AttributeError:
                    # {'if_name' : { attributes...}}, {'if_other': {...}}
                    if_name = interface
                    if_attributes = interface_list[if_name]

                if 'port' in if_attributes:
                    rem_port_keys = ('lldp_rem_port_id_subtype',
                                     'lldp_rem_port_id',
                                     'lldp_rem_port_desc',
                                     'lldp_rem_max_frame_size',
                                     'lldp_rem_agg_port_id')
                    parsed_port = list(zip(rem_port_keys, self.parse_port(if_attributes['port'])))
                    parsed_interfaces[if_name].update(parsed_port)

                chassis_id = ''

                if 'chassis' in if_attributes:
                    rem_chassis_keys = ('lldp_rem_chassis_id_subtype',
                                        'lldp_rem_chassis_id',
                                        'lldp_rem_sys_name',
                                        'lldp_rem_sys_desc',
                                        'lldp_rem_man_addr',
                                        'lldp_rem_ttl')
                    parsed_chassis = list(zip(rem_chassis_keys,
                                         self.parse_chassis(if_attributes['chassis'])))
                    parsed_interfaces[if_name].update(parsed_chassis)
                    chassis_id = parsed_chassis[1][1]

                parsed_interfaces[if_name].update({
                    'lldp_rem_port_vlan_id': self.parse_pvid(if_attributes),
                    'lldp_rem_custom_tlvs': self.parse_unknown_tlvs(if_attributes),
                    'lldp_rem_med_inv_serial': self.parse_med_serial(if_attributes),
                })

                # lldpRemTimeMark           TimeFilter,
                parsed_interfaces[if_name].update({'lldp_rem_time_mark':
                                                   str(parse_time(if_attributes.get('age')))})

                # lldpRemIndex
                parsed_interfaces[if_name].update({'lldp_rem_index': str(if_attributes.get('rid'))})

                capability_list = self.get_sys_capability_list(if_attributes, if_name, chassis_id)
                # lldpSysCapSupported
                parsed_interfaces[if_name].update({'lldp_rem_sys_cap_supported':
                                                   self.parse_sys_capabilities(capability_list)})
                # lldpSysCapEnabled
                parsed_interfaces[if_name].update({'lldp_rem_sys_cap_enabled':
                                                   self.parse_sys_capabilities(
                                                       capability_list, enabled=True)})
            if lldp_json.get('lldp_loc_chassis'):
                loc_chassis_keys = ('lldp_loc_chassis_id_subtype',
                                    'lldp_loc_chassis_id',
                                    'lldp_loc_sys_name',
                                    'lldp_loc_sys_desc',
                                    'lldp_loc_man_addr',
                                    'lldp_loc_ttl')
                parsed_chassis = dict(zip(loc_chassis_keys,
                                     self.parse_chassis(lldp_json['lldp_loc_chassis']
                                                        ['local-chassis']['chassis'])))

                loc_capabilities = self.get_sys_capability_list(lldp_json['lldp_loc_chassis']
                                                                ['local-chassis'], 'local', 'chassis')
                # lldpLocSysCapSupported
                parsed_chassis.update({'lldp_loc_sys_cap_supported':
                                      self.parse_sys_capabilities(loc_capabilities)})
                # lldpLocSysCapEnabled
                parsed_chassis.update({'lldp_loc_sys_cap_enabled':
                                      self.parse_sys_capabilities(loc_capabilities, enabled=True)})

                parsed_interfaces['local-chassis'].update(parsed_chassis)

            return parsed_interfaces
        except (KeyError, ValueError):
            logger.exception("Failed to parse LLDPd JSON. \n{}\n -- ".format(lldp_json))

    def parse_chassis(self, chassis_attributes):
        try:
            if 'id' in chassis_attributes and 'id' not in chassis_attributes['id']:
                sys_name = ''
                attributes = chassis_attributes
                id_attributes = chassis_attributes['id']
            else:
                (sys_name, attributes) = list(chassis_attributes.items())[0]
                id_attributes = attributes.get('id', '')

            chassis_id_subtype = str(self.ChassisIdSubtypeMap[id_attributes['type']].value)
            chassis_id = id_attributes.get('value', '')
            descr = attributes.get('descr', '')
            mgmt_ip = attributes.get('mgmt-ip', '')
            if isinstance(mgmt_ip, list):
                mgmt_ip = ','.join(mgmt_ip)
            ttl = attributes.get('ttl', '')
            if ttl is None:
                ttl = ''
            ttl = str(ttl)
        except (KeyError, ValueError):
            logger.exception("Could not infer system information from: {}"
                             .format(chassis_attributes))
            chassis_id_subtype = chassis_id = sys_name = descr = mgmt_ip = ttl = ''

        return (chassis_id_subtype,
                chassis_id,
                sys_name,
                descr,
                mgmt_ip,
                ttl,
                )

    def parse_port(self, port_attributes):
        port_identifiers = port_attributes.get('id')
        try:
            subtype = str(self.PortIdSubtypeMap[port_identifiers['type']].value)
            value = port_identifiers['value']

        except ValueError:
            logger.exception("Could not infer chassis subtype from: {}".format(port_attributes))
            subtype, value = None, None

        mfs = port_attributes.get('mfs', '')
        if mfs is None:
            mfs = ''
        aggregid = port_attributes.get('aggregation', '')
        if aggregid is None:
            aggregid = ''

        return (subtype,
                value,
                port_attributes.get('descr', ''),
                str(mfs),
                str(aggregid),
                )

    def parse_pvid(self, if_attributes):
        """Return IEEE 802.1 Port VLAN ID when the VLAN entry is marked pvid."""
        for entry in _as_list(if_attributes.get('vlan')):
            if not isinstance(entry, dict):
                continue
            pvid = entry.get('pvid')
            if pvid is True or str(pvid).lower() == 'true':
                vlan_id = entry.get('vlan-id', '')
                if vlan_id is None:
                    return ''
                return str(vlan_id)
        return ''

    def parse_unknown_tlvs(self, if_attributes):
        """Serialize neighbor org-specific TLVs for APPL_DB (JSON array)."""
        unknown = if_attributes.get('unknown-tlvs')
        if not unknown:
            return ''
        if isinstance(unknown, dict):
            tlvs = unknown.get('unknown-tlv', unknown)
        else:
            tlvs = unknown
        parsed = []
        for tlv in _as_list(tlvs):
            if not isinstance(tlv, dict):
                continue
            oui = _normalize_oui(tlv.get('oui', ''))
            subtype = tlv.get('subtype', '')
            if subtype is None:
                subtype = ''
            value_hex = _hex_bytes_to_string(tlv.get('value', ''))
            if not oui and not value_hex:
                continue
            parsed.append({
                'type': LLDP_ORG_SPECIFIC_TLV_TYPE,
                'oui': oui,
                'oui-subtype': str(subtype),
                'value': value_hex,
            })
        if not parsed:
            return ''
        return json.dumps(parsed, separators=(',', ':'))

    def parse_med_serial(self, if_attributes):
        """LLDP-MED Inventory serial number, if the peer advertised it."""
        med = if_attributes.get('lldp-med')
        if isinstance(med, dict):
            inventory = med.get('inventory')
            if isinstance(inventory, dict) and inventory.get('serial'):
                return str(inventory.get('serial'))
        chassis = if_attributes.get('chassis')
        if isinstance(chassis, dict):
            for chassis_body in chassis.values():
                if not isinstance(chassis_body, dict):
                    continue
                inventory = chassis_body.get('inventory')
                if isinstance(inventory, dict) and inventory.get('serial'):
                    return str(inventory.get('serial'))
        return ''

    def cache_diff(self, cache, update):
        """
        Find difference in keys between update and local cache dicts
        :param cache: Local cache dict
        :param update: Update dict
        :return: new, changed, deleted keys tuple
        """
        new_keys = list(set(update.keys()) - set(cache.keys()))
        changed_keys = list(set(key for key in set(update.keys()) & set(cache.keys()) if update[key] != cache[key]))
        deleted_keys = list(set(cache.keys()) - set(update.keys()))

        return new_keys, changed_keys, deleted_keys

    def is_only_time_mark_modified(self, cached_interface, updated_interface):
        """
        Check if only lldp_rem_time_mark is modified in the update
        :param cached_interface: Local cached interface dict
        :param updated_interface: Updated interface dict
        :return: True if only lldp_rem_time_mark is modified, False otherwise
        """
        if len(cached_interface) != len(updated_interface):
            return False

        changed_keys = 0

        for key in cached_interface.keys():
            if 'lldp_rem_time_mark' == key and cached_interface[key] != updated_interface[key]:
                changed_keys += 1
            elif key not in updated_interface or cached_interface[key] != updated_interface[key]:
                return False

        return True if changed_keys == 1 else False

    @staticmethod
    def _stat_value(container, key):
        """Extract one lldpd JSON counter (string, int, or {key: value})."""
        if not isinstance(container, dict):
            return None
        val = container.get(key)
        if isinstance(val, dict):
            inner = val.get(key)
            if inner is None:
                inner = val.get('value')
            val = inner
        if val is None or isinstance(val, bool):
            return None
        return str(val)

    def _counters_from_container(self, container):
        counters = {}
        for src, dst in self.LLDP_STAT_FIELD_MAP:
            val = self._stat_value(container, src)
            counters[dst] = val if val is not None else '0'
        return counters

    def _sum_counters(self, entries):
        totals = {dst: 0 for _, dst in self.LLDP_STAT_FIELD_MAP}
        for entry in entries:
            for dst in totals:
                try:
                    totals[dst] += int(entry.get(dst, 0) or 0)
                except (TypeError, ValueError):
                    pass
        return {key: str(val) for key, val in totals.items()}

    def parse_statistics(self, stats_json):
        """Parse `lldpcli -f json show statistics` into COUNTERS_DB hashes."""
        if not stats_json:
            return None
        lldp_root = stats_json.get('lldp', stats_json)
        if not isinstance(lldp_root, dict):
            return None

        parsed = {}
        interface_list = lldp_root.get('interface') or []
        for interface in _as_list(interface_list):
            if_name = None
            if_attributes = None
            if isinstance(interface, dict) and 'name' in interface and (
                    'tx' in interface or 'rx' in interface):
                if_name = str(interface.get('name'))
                if_attributes = interface
            else:
                try:
                    (if_name, if_attributes), = interface.items()
                except (AttributeError, ValueError, TypeError):
                    if not isinstance(interface_list, dict):
                        continue
                    if_name = interface
                    if_attributes = interface_list.get(if_name)
            if not if_name or not isinstance(if_attributes, dict):
                continue
            if re.match(SONIC_ETHERNET_RE_PATTERN, if_name) is None:
                logger.warning("Ignoring statistics for interface '{}'".format(if_name))
                continue
            parsed[if_name] = self._counters_from_container(if_attributes)

        summary = lldp_root.get('summary')
        if parsed:
            parsed[self.LLDP_STATISTICS_GLOBAL_KEY] = self._sum_counters(
                [v for k, v in parsed.items() if k != self.LLDP_STATISTICS_GLOBAL_KEY])
        elif isinstance(summary, dict):
            parsed[self.LLDP_STATISTICS_GLOBAL_KEY] = self._counters_from_container(summary)
        else:
            return None
        return parsed

    def sync_statistics(self, parsed_stats):
        """Write parsed LLDP counters to COUNTERS_DB LLDP_STATISTICS."""
        if not parsed_stats:
            return
        logger.debug("Initiating LLDP statistics sync to COUNTERS_DB...")
        new, changed, deleted = self.cache_diff(self.stats_cache, parsed_stats)
        for if_name in list(new) + list(changed):
            table_key = ':'.join([self.LLDP_STATISTICS_TABLE, if_name])
            self.db_connector.hmset(self.db_connector.COUNTERS_DB, table_key, parsed_stats[if_name])
            logger.debug("sync'd statistics {}: {}".format(table_key, parsed_stats[if_name]))
        for if_name in deleted:
            table_key = ':'.join([self.LLDP_STATISTICS_TABLE, if_name])
            self.db_connector.delete(self.db_connector.COUNTERS_DB, table_key)
            logger.info("Delete statistics table_key: {}".format(table_key))
        self.stats_cache = parsed_stats

    def sync(self, parsed_update):
        """
        Sync LLDP information to redis DB.
        """
        logger.debug("Initiating LLDPd sync to Redis...")

        # push local chassis data to APP DB
        if 'local-chassis' in parsed_update:
            chassis_update = parsed_update.pop('local-chassis')
            if chassis_update != self.chassis_cache:
                self.db_connector.delete(self.db_connector.APPL_DB,
                                         LldpSyncDaemon.LLDP_LOC_CHASSIS_TABLE)
                for k, v in chassis_update.items():
                    self.db_connector.set(self.db_connector.APPL_DB,
                                          LldpSyncDaemon.LLDP_LOC_CHASSIS_TABLE, k, v, blocking=True)
                self.chassis_cache = chassis_update
                logger.debug("sync'd: {}".format(json.dumps(chassis_update, indent=3)))

        new, changed, deleted = self.cache_diff(self.interfaces_cache, parsed_update)

        if new or deleted:
            # If detects any new or deleted interfaces, repopulate for changed interfaces
            for interface in changed:
                if re.match(SONIC_ETHERNET_RE_PATTERN, interface) is None:
                    logger.warning("Ignoring interface '{}'".format(interface))
                    continue
                table_key = ':'.join([LldpSyncDaemon.LLDP_ENTRY_TABLE, interface])
                self.db_connector.delete(self.db_connector.APPL_DB, table_key)
                self.db_connector.hmset(self.db_connector.APPL_DB, table_key, parsed_update[interface])
                logger.info("Force repopulate the changed interface {} : {}".format(interface, parsed_update[interface]))
        else:
            # For changed elements, if only lldp_rem_time_mark changed, update its value, otherwise delete and repopulate
            for interface in changed:
                if re.match(SONIC_ETHERNET_RE_PATTERN, interface) is None:
                    logger.warning("Ignoring interface '{}'".format(interface))
                    continue
                table_key = ':'.join([LldpSyncDaemon.LLDP_ENTRY_TABLE, interface])
                if self.is_only_time_mark_modified(self.interfaces_cache[interface], parsed_update[interface]):
                    self.db_connector.set(self.db_connector.APPL_DB, table_key, 'lldp_rem_time_mark', parsed_update[interface]['lldp_rem_time_mark'], blocking=True)
                    logger.debug("Only sync'd interface {} lldp_rem_time_mark: {}".format(interface, parsed_update[interface]['lldp_rem_time_mark']))
                else:
                    self.db_connector.delete(self.db_connector.APPL_DB, table_key)
                    self.db_connector.hmset(self.db_connector.APPL_DB, table_key, parsed_update[interface])
                    logger.info("Repopulate for changed interface {} : {}".format(interface, parsed_update[interface]))
        self.interfaces_cache = parsed_update
        # Delete LLDP_ENTRIES which are missing
        for interface in deleted:
            table_key = ':'.join([LldpSyncDaemon.LLDP_ENTRY_TABLE, interface])
            self.db_connector.delete(self.db_connector.APPL_DB, table_key)
            logger.info("Delete table_key: {}".format(table_key))
        # Repopulate LLDP_ENTRY_TABLE by adding new elements
        for interface in new:
            if re.match(SONIC_ETHERNET_RE_PATTERN, interface) is None:
                logger.warning("Ignoring interface '{}'".format(interface))
                continue
            # port_table_key = LLDP_ENTRY_TABLE:INTERFACE_NAME;
            table_key = ':'.join([LldpSyncDaemon.LLDP_ENTRY_TABLE, interface])
            self.db_connector.hmset(self.db_connector.APPL_DB, table_key, parsed_update[interface])
            logger.info("Add new interface {} : {}".format(interface, parsed_update[interface]))

        if self._pending_statistics is not None:
            self.sync_statistics(self._pending_statistics)
            self._pending_statistics = None
