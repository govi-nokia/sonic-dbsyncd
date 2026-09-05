# MONKEY PATCH!!!
import json
import os
import sys

import mockredis
import redis
from swsscommon.swsscommon import SonicV2Connector
from swsssdk import SonicDBConfig
from swsssdk.interface import DBInterface
from swsscommon import swsscommon


if sys.version_info >= (3, 0):
    long = int
    xrange = range
    basestring = str


def _subscribe_keyspace_notification(self, db_name, client):
    pass


def config_set(self, *args):
    pass


class MockPubSub:
    def get_message(self):
        return None

    def psubscribe(self, *args, **kwargs):
        pass

    def punsubscribe(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def listen(self):
        return []

INPUT_DIR = os.path.dirname(os.path.abspath(__file__))


class SwssSyncClient(mockredis.MockRedis):
    def __init__(self, *args, **kwargs):
        super(SwssSyncClient, self).__init__(strict=True, *args, **kwargs)
        db = kwargs.pop('db')
        self.decode_responses = kwargs.pop('decode_responses', False) == True

        self.pubsub = MockPubSub()

        if db == 0:
            with open(INPUT_DIR + '/LLDP_ENTRY_TABLE.json') as f:
                db = json.load(f)
                for h, table in db.items():
                    for k, v in table.items():
                        self.hset(h, k, v)

        elif db == 4:
            with open(INPUT_DIR + '/CONFIG_DB.json') as f:
                db = json.load(f)
                for h, table in db.items():
                    for k, v in table.items():
                        self.hset(h, k, v)

        # COUNTERS_DB (id 2) starts empty; LLDP_STATISTICS hashes are written
        # by LldpSyncDaemon.sync_statistics() during tests.

    # Patch mockredis/mockredis/client.py
    # The offical implementation assume decode_responses=False
    # Here we detect the option and decode after doing encode
    def _encode(self, value):
        "Return a bytestring representation of the value. Taken from redis-py connection.py"

        value = super(SwssSyncClient, self)._encode(value)

        if self.decode_responses:
            return value.decode('utf-8')

    # Patch mockredis/mockredis/client.py
    # The official implementation will filter out keys with a slash '/'
    # ref: https://github.com/locationlabs/mockredis/blob/master/mockredis/client.py
    def keys(self, pattern='*'):
        """Emulate keys."""
        import fnmatch
        import re

        # making sure the pattern is unicode/str.
        try:
            pattern = pattern.decode('utf-8')
            # This throws an AttributeError in python 3, or an
            # UnicodeEncodeError in python 2
        except (AttributeError, UnicodeEncodeError):
            pass

        # Make regex out of glob styled pattern.
        regex = fnmatch.translate(pattern)
        regex = re.compile(regex)

        # Find every key that matches the pattern
        return [key for key in self.redis.keys() if regex.match(key)]

class MockConnector(object):
    APPL_DB = 0
    CONFIG_DB = 4
    COUNTERS_DB = 2
    data = {}

    def __init__(self):
        pass

    def _store(self, db_id):
        if db_id not in MockConnector.data:
            MockConnector.data[db_id] = {}
        return MockConnector.data[db_id]

    def connect(self, db_id):
        store = self._store(db_id)
        if db_id == 0:
            with open(INPUT_DIR + '/LLDP_ENTRY_TABLE.json') as f:
                db = json.load(f)
                for h, table in db.items():
                    store[h] = {}
                    for k, v in table.items():
                        store[h][k] = v

        elif db_id == 4:
            with open(INPUT_DIR + '/CONFIG_DB.json') as f:
                db = json.load(f)
                for h, table in db.items():
                    store[h] = {}
                    for k, v in table.items():
                        store[h][k] = v

        # COUNTERS_DB (id 2) starts empty; LLDP_STATISTICS hashes are written
        # by LldpSyncDaemon.sync_statistics() during tests.

    def get(self, db_id, key, field):
        return self._store(db_id)[key][field]

    def keys(self, db_id):
        return list(self._store(db_id).keys())

    def get_all(self, db_id, key):
        return self._store(db_id)[key]

    def exists(self, db_id, key):
        return key in self._store(db_id)

    def set(self, db_id, key, field, value, blocking=False):
        store = self._store(db_id)
        store[key] = {}
        store[key][field] = value

    def hmset(self, db_id, key, fieldsvalues):
        store = self._store(db_id)
        store[key] = {}
        for field, value in fieldsvalues.items():
            store[key][field] = value

    def delete(self, db_id, key):
        del self._store(db_id)[key]


DBInterface._subscribe_keyspace_notification = _subscribe_keyspace_notification
mockredis.MockRedis.config_set = config_set
redis.StrictRedis = SwssSyncClient
SonicV2Connector.connect = MockConnector.connect
swsscommon.SonicV2Connector = MockConnector
