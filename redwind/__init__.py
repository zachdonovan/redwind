import sys
import importlib

sys.path.append('external')

from flask import Flask
from flask.ext.sqlalchemy import SQLAlchemy
from flask.ext.login import LoginManager
from flask.ext.themes2 import Themes

from werkzeug.datastructures import ImmutableDict
from werkzeug.contrib.fixers import ProxyFix
from logging import FileHandler
from config import Configuration
import logging


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app)
app.config.from_object(Configuration)

app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True
app.jinja_env.add_extension('jinja2.ext.autoescape')
app.jinja_env.add_extension('jinja2.ext.with_')
app.jinja_env.add_extension('jinja2.ext.i18n')

db = SQLAlchemy(app)

# toolbar = DebugToolbarExtension(app)
login_mgr = LoginManager(app)
login_mgr.login_view = 'login'

themes = Themes(app)

if app.config.get('PROFILE'):
    from werkzeug.contrib.profiler import ProfilerMiddleware
    f = open('logs/profiler.log', 'w')
    app.wsgi_app = ProfilerMiddleware(app.wsgi_app, f, restrictions=[60],
                                      sort_by=('cumtime', 'tottime', 'ncalls'))


# logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)
if not app.debug:
    app.logger.setLevel(logging.DEBUG)
    stream_handler = FileHandler("/var/log/gunicorn/redwind.log")
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(formatter)
    app.logger.addHandler(stream_handler)


for handler in ['views', 'services', 'micropub']:
    importlib.import_module('redwind.' + handler)


for plugin in [
        'facebook',
        'instagram',
        'locations',
        'push',
        'twitter',
        'wm_receiver',
        'wm_sender',
        'wordpress',
]:
    # app.logger.info('loading plugin module %s', plugin)
    module = importlib.import_module('redwind.plugins.' + plugin)
    try:
        module.register()
    except:
        app.logger.warn('no register method for plugin module %s', plugin)
