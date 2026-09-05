"""
LLDP 802.1AB - Textual Conventions (Enumerated Types)

See:
http://www.ieee802.org/1/files/public/MIBs/LLDP-MIB-200505060000Z.txt
"""

from enum import unique, Enum


@unique
class LldpChassisIdSubtype(int, Enum):
    """
    LldpChassisIdSubtype ::= TEXTUAL-CONVENTION
        SYNTAX  INTEGER {
            chassisComponent(1),
            interfaceAlias(2),
            portComponent(3),
            macAddress(4),
            networkAddress(5),
            interfaceName(6),
            local(7)
        }
    """
    chassisComponent = 1
    interfaceAlias = 2
    portComponent = 3
    macAddress = 4
    networkAddress = 5
    interfaceName = 6
    local = 7


@unique
class LldpPortIdSubtype(int, Enum):
    """
    LldpPortIdSubtype ::= TEXTUAL-CONVENTION
        SYNTAX  INTEGER {
                interfaceAlias(1),
                portComponent(2),
                macAddress(3),
                networkAddress(4),
                interfaceName(5),
                agentCircuitId(6),
                local(7)
        }
    """
    interfaceAlias = 1
    portComponent = 2
    macAddress = 3
    networkAddress = 4
    interfaceName = 5
    agentCircuitId = 6
    local = 7


@unique
class LldpManAddrIfSubtype(int, Enum):
    """
    LldpManAddrIfSubtype ::= TEXTUAL-CONVENTION
        SYNTAX  INTEGER {
                unknown(1),
                ifIndex(2),
                systemPortNumber(3)
        }
    """
    unknown = 1
    ifIndex = 2
    systemPortNumber = 3


@unique
class LldpSystemCapabilitiesMap(int, Enum):
    """
    LldpSystemCapabilitiesMap::= TEXTUAL - CONVENTION
    SYNTAX  BITS {
            other(0),
            repeater(1),
            bridge(2),
            wlanAccessPoint(3),
            router(4),
            telephone(5),
            docsisCableDevice(6),
            stationOnly(7),
            cVlan(8),
            sVlan(9),
            twoPortMacRelay(10)
    }
    """
    other = 0
    repeater = 1
    bridge = 2
    wlanAccessPoint = 3
    router = 4
    telephone = 5
    docsisCableDevice = 6
    stationOnly = 7
    cVlan = 8
    sVlan = 9
    twoPortMacRelay = 10


# lldpd JSON uses short labels ("Wlan", "Station", "Tel") that do not match
# the IEEE/SNMP enumerant names. Map both onto the same bit index.
LLDP_CAPABILITY_NAME_TO_BIT = {
    "other": 0,
    "repeater": 1,
    "bridge": 2,
    "mac_bridge": 2,
    "mac-bridge": 2,
    "wlanaccesspoint": 3,
    "wlan": 3,
    "wlan_access_point": 3,
    "router": 4,
    "telephone": 5,
    "tel": 5,
    "docsiscabledevice": 6,
    "docsis": 6,
    "stationonly": 7,
    "station": 7,
    "cvlan": 8,
    "c-vlan": 8,
    "c_vlan": 8,
    "svlan": 9,
    "s-vlan": 9,
    "s_vlan": 9,
    "twoportmacrelay": 10,
    "two-port mac relay": 10,
    "two_port_mac_relay": 10,
    "tpmr": 10,
}
