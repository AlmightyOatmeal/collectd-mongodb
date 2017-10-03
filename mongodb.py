#
# Collectd Plugin for MongoDB Stats
#

import collectd
import pymongo
from distutils.version import StrictVersion as V


class MongoDB(object):
    def __init__(self):
        self.plugin_name = "mongo"
        self.mongo_host = "127.0.0.1"
        self.mongo_port = None
        self.mongo_db = ["admin", ]
        self.mongo_user = None
        self.mongo_password = None
        self.mongo_version = None
        self.cluster_name = None
        self.dimensions = None

        self.use_ssl = False
        self.ca_certs_path = None
        self.ssl_client_cert_path = None
        self.ssl_client_key_path = None
        self.ssl_client_key_passphrase = None

    def submit(self, type, type_instance, value, db=None):
        v = collectd.Values()
        v.plugin = self.plugin_name

        # discovered dimensions
        discovered_dims = None
        if self.cluster_name is not None and db is not None:
            discovered_dims = 'cluster=%s,db=%s' % (self.cluster_name, db)
        elif self.cluster_name is not None:
            discovered_dims = 'cluster=%s' % self.cluster_name
        elif db is not None:
            discovered_dims = 'db=%s' % db

        # set plugin_instance
        if self.dimensions is not None and discovered_dims is not None:
            v.plugin_instance = '%s[%s,%s]' % (self.mongo_port,
                                               self.dimensions,
                                               discovered_dims)
        elif self.dimensions is not None:
            v.plugin_instance = '%s[%s]' % (self.mongo_port, self.dimensions)
        elif discovered_dims is not None:
            v.plugin_instance = '%s[%s]' % (self.mongo_port, discovered_dims)
        else:
            v.plugin_instance = '%s' % self.mongo_port

        v.type = type
        v.type_instance = type_instance
        v.values = [value, ]

        # With some versions of CollectD, a dummy metadata map must be added
        # to each value for it to be correctly serialized to JSON by the
        # write_http plugin. See
        # https://github.com/collectd/collectd/issues/716
        v.meta = {'0': True}

        v.dispatch()

    @property
    def ssl_kwargs(self):
        d = {}
        if self.use_ssl:
            d["ssl"] = True
            if self.ca_certs_path:
                d["ssl_ca_certs"] = self.ca_certs_path
            if self.ssl_client_cert_path:
                d["ssl_certfile"] = self.ssl_client_cert_path
            if self.ssl_client_key_path:
                d["ssl_keyfile"] = self.ssl_client_key_path
            if self.ssl_client_key_passphrase:
                d["ssl_pem_passphrase"] = self.ssl_client_key_passphrase

        return d

    def do_server_status(self):
        try:
            con = pymongo.MongoClient(self.mongo_host, self.mongo_port,
                                      **self.ssl_kwargs)
        except Exception, e:
            self.log('ERROR: Connection failed for %s:%s' % (
                self.mongo_host, self.mongo_port))
        db = con[self.mongo_db[0]]
        if self.mongo_user and self.mongo_password:
            db.authenticate(self.mongo_user, self.mongo_password)
        elif self.ssl_client_cert_path:
            try:
                db.authenticate(self.mongo_user, mechanism='MONGODB-X509')
            except pymongo.helpers.OperationFailure as e:
                collectd.error(str(e))
                collectd.error("ERROR: Could not authenticate to Mongo using TLS client "
                               "auth username '%s'.  Make sure this username is set and matches "
                               "EXACTLY (fields in the same order as) the user specified in the "
                               "$external database" % self.mongo_user)
                return

        server_status = db.command('serverStatus')

        # mongodb version
        self.mongo_version = server_status['version']
        at_least_2_4 = V(self.mongo_version) >= V('2.4.0')
        eq_gt_3_0 = V(self.mongo_version) >= V('3.0.0')

        # cluster discovery,repl lag
        rs_status = {}
        slaveDelays = {}
        try:
            rs_status = con.admin.command("replSetGetStatus")
            is_primary_node = 0
            active_nodes = 0
            primary_node = None
            host_node = None

            if 'set' in rs_status and self.cluster_name is None:
                self.cluster_name = rs_status['set']

            rs_conf = con.local.system.replset.find_one()
            for member in rs_conf['members']:
                if member.get('slaveDelay') is not None:
                    slaveDelays[member['host']] = member.get('slaveDelay')
                else:
                    slaveDelays[member['host']] = 0

            if 'members' in rs_status:
                for member in rs_status['members']:
                    if member['health'] == 1:
                        active_nodes += 1
                    if member['stateStr'] == "PRIMARY":
                        primary_node = member
                    if member.get('self') is True:
                        host_node = member
                if host_node["stateStr"] == "PRIMARY":
                    maximal_lag = 0
                    is_primary_node = 1
                    for member in rs_status['members']:
                        if not member['stateStr'] == "ARBITER":
                            lastSlaveOpTime = member['optimeDate']
                            replicationLag = \
                                abs(primary_node["optimeDate"] -
                                    lastSlaveOpTime).seconds - \
                                slaveDelays[member['name']]
                            maximal_lag = max(maximal_lag, replicationLag)
                    self.submit('gauge', 'repl.max_lag', maximal_lag)
            self.submit('gauge', 'repl.active_nodes', active_nodes)
            self.submit('gauge', 'repl.is_primary_node', is_primary_node)
        except pymongo.errors.OperationFailure, e:
            if str(e).find('not running with --replSet'):
                self.log("server not running with --replSet")
                pass
            else:
                pass

        # uptime
        if 'uptime' in server_status:
            self.submit('gauge', 'uptime',
                        server_status['uptime'])

        # operations
        opcounters_obj = server_status.get('opcounters')
        if opcounters_obj:
            for counter_key in opcounters_obj.keys():
                self.submit('counter', 'opcounters.{}'.format(counter_key),
                            opcounters_obj[counter_key])

        # memory
        mem_obj = server_status.get('mem')
        if mem_obj:
            for t in ['resident', 'virtual', 'mapped']:
                if t not in mem_obj:
                    continue

                self.submit('gauge', 'mem.{}'.format(t),
                            mem_obj[t])

        # network
        network_obj = server_status.get('network')
        if network_obj:
            for t in ['bytesIn', 'bytesOut', 'numRequests']:
                if t not in network_obj:
                    continue

                self.submit('counter', 'network.{}'.format(t),
                            network_obj[t])

        # connections
        connections_obj = server_status.get('connections')
        if connections_obj:
            for t in ['current', 'available', 'totalCreated']:
                if t not in connections_obj:
                    continue

                self.submit('gauge', 'connections.{}'.format(t),
                            connections_obj[t])

        # background flush
        backgroundflushing_obj = server_status.get('backgroundFlushing')
        if backgroundflushing_obj:
            for t in ['flushes', 'average_ms', 'last_ms']:
                if t not in backgroundflushing_obj:
                    continue

                self.submit('counter', 'backgroundFlushing.{}'.format(t),
                            backgroundflushing_obj[t])

        # asserts
        asserts_obj = server_status.get('asserts')
        if asserts_obj:
            for t in ['regular', 'warning']:
                if t not in asserts_obj:
                    continue

                self.submit('counter', 'asserts.{}'.format(t),
                            asserts_obj[t])

        # page faults
        extra_info_obj = server_status.get('extra_info')
        if extra_info_obj:
            for t in ['page_faults', 'heap_usage_bytes']:
                if t not in extra_info_obj:
                    continue

                self.submit('counter', 'extra_info.{}'.format(t),
                            extra_info_obj[t])

        lock_type = {'R': 'read', 'W': 'write', 'r': 'intentShared',
                     'w': 'intentExclusive'}
        lock_metric_type = {'deadlockCount': 'counter',
                            'acquireCount': 'counter',
                            'timeAcquiringMicros': 'gauge',
                            'acquireWaitCount': 'gauge',
                            'timeLockedMicros': 'counter',
                            'currentQueue': 'gauge',
                            'activeClients': 'gauge'}

        # globalLocks
        globallocks_obj = server_status.get('globalLock')
        if globallocks_obj:
            for t in ['currentQueue', 'activeClients']:
                if t not in globallocks_obj:
                    continue

                if t not in lock_metric_type:
                    continue

                for item_key in globallocks_obj[t].keys():
                    self.submit(lock_metric_type[t],
                                'globalLock.{}.{}'.format(t, item_key),
                                globallocks_obj[t][item_key])

        # locks for version 3.x
        if eq_gt_3_0 and 'locks' in server_status:
            for lock_stat in ('deadlockCount', 'acquireCount',
                              'timeAcquiringMicros', 'acquireWaitCount'):
                if lock_stat in server_status['locks']['Global']:
                    for k, v in \
                            server_status['locks']['Global'][lock_stat]\
                            .items():
                        if k in lock_type and lock_stat in lock_metric_type:
                            self.submit(lock_metric_type[lock_stat],
                                        'lock.Global.%s.%s' % (
                                            lock_stat, lock_type[k]), v)

            for lock_stat in ('deadlockCount', 'acquireCount',
                              'timeAcquiringMicros', 'acquireWaitCount'):
                if lock_stat in server_status['locks']['Database']:
                    for k, v in \
                            server_status['locks']['Database'][lock_stat]\
                            .items():
                        if k in lock_type and lock_stat in lock_metric_type:
                            self.submit(lock_metric_type[lock_stat],
                                        'lock.Database.%s.%s' % (
                                            lock_stat, lock_type[k]), v)

        elif at_least_2_4 and 'locks' in server_status:
            # locks for version 2.x
            for lock_stat in ('timeLockedMicros', 'timeAcquiringMicros'):
                if lock_stat in server_status['locks']['.']:
                    for k, v in server_status['locks']['.'][lock_stat].items():
                        if k in lock_type and lock_stat in lock_metric_type:
                            self.submit(lock_metric_type[lock_stat],
                                        'lock.Global.%s.%s' % (
                                            lock_stat, lock_type[k]), v)

        # indexes for version 2.x
        if 'indexCounters' in server_status:
            index_counters = server_status['indexCounters'] if at_least_2_4 \
                else server_status['indexCounters']['btree']
            for t in ['accesses', 'misses', 'hits', 'resets', 'missRatio']:
                self.submit('counter', 'indexCounters.' + t, index_counters[t])

        for mongo_db in self.mongo_db:
            db = con[mongo_db]
            if self.mongo_user and self.mongo_password:
                con[self.mongo_db[0]].authenticate(self.mongo_user,
                                                   self.mongo_password)
            db_stats = db.command('dbstats')

            # stats counts
            self.submit('gauge', 'objects',
                        db_stats['objects'], mongo_db)
            self.submit('gauge', 'collections',
                        db_stats['collections'], mongo_db)
            self.submit('gauge', 'numExtents',
                        db_stats['numExtents'], mongo_db)
            self.submit('gauge', 'indexes',
                        db_stats['indexes'], mongo_db)

            # stats sizes
            self.submit('gauge', 'storageSize',
                        db_stats['storageSize'], mongo_db)
            self.submit('gauge', 'indexSize',
                        db_stats['indexSize'], mongo_db)
            self.submit('gauge', 'dataSize',
                        db_stats['dataSize'], mongo_db)

        # repl operations
        if 'opcountersRepl' in server_status:
            for k, v in server_status['opcountersRepl'].items():
                self.submit('counter', 'opcountersRepl.' + k, v)

        con.close()

    def log(self, msg):
        collectd.info('mongodb plugin: %s' % msg)

    def config(self, obj):
        for node in obj.children:
            if node.key == 'Port':
                self.mongo_port = int(node.values[0])
            elif node.key == 'Host':
                self.mongo_host = node.values[0]
            elif node.key == 'User':
                self.mongo_user = node.values[0]
            elif node.key == 'Password':
                self.mongo_password = node.values[0]
            elif node.key == 'Database':
                self.mongo_db = node.values
            elif node.key == 'Dimensions':
                self.dimensions = node.values[0]
            elif node.key == 'UseTLS':
                self.use_ssl = node.values[0]
            elif node.key == 'CACerts':
                self.ca_certs_path = node.values[0]
            elif node.key == 'TLSClientCert':
                self.ssl_client_cert_path = node.values[0]
            elif node.key == 'TLSClientKey':
                self.ssl_client_key_path = node.values[0]
            elif node.key == 'TLSClientKeyPassphrase':
                self.ssl_client_key_passphrase = node.values[0]
            else:
                self.log("Unknown configuration key %s" % node.key)

def config(obj):
    mongodb = MongoDB()
    mongodb.config(obj)
    collectd.register_read(mongodb.do_server_status,
                           name='mongo-%s:%s' % (mongodb.mongo_host,
                                                 mongodb.mongo_port))

collectd.register_config(config)
