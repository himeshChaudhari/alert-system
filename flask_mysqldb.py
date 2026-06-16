import pymysql
pymysql.install_as_MySQLdb()

from flask import g, current_app

class MySQL(object):
    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        app.teardown_appcontext(self.teardown)

    def connect(self):
        config = {}
        
        mappings = {
            'MYSQL_HOST': 'host',
            'MYSQL_USER': 'user',
            'MYSQL_PASSWORD': 'password',
            'MYSQL_DB': 'database',
            'MYSQL_PORT': 'port',
            'MYSQL_UNIX_SOCKET': 'unix_socket',
            'MYSQL_CONNECT_TIMEOUT': 'connect_timeout',
            'MYSQL_CHARSET': 'charset',
            'MYSQL_SSL': 'ssl'
        }
        
        for flask_key, pymysql_key in mappings.items():
            val = current_app.config.get(flask_key)
            if val is not None:
                if flask_key == 'MYSQL_PORT':
                    val = int(val)
                elif flask_key == 'MYSQL_CONNECT_TIMEOUT':
                    val = int(val)
                config[pymysql_key] = val

        if 'charset' not in config:
            config['charset'] = 'utf8mb4'
            
        return pymysql.connect(**config)

    @property
    def connection(self):
        if not hasattr(g, 'mysqldb_db'):
            g.mysqldb_db = self.connect()
        return g.mysqldb_db

    def teardown(self, exception):
        db = getattr(g, 'mysqldb_db', None)
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
