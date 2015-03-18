import os


class Configuration(object):
    DEBUG = False

    # optionally, bypass indieauth
    # BYPASS_INDIEAUTH = True

    # do not intercept redirects when using debug toolbar
    DEBUG_TB_INTERCEPT_REDIRECTS = False

    # Some secret key used by Flask-Login
    SECRET_KEY = os.urandom(24)

    # Color theme for pygments syntax coloring (handled by the
    # codehilite plugin for Markdown)
    PYGMENTS_STYLE = 'tango'

    # schema to contact DB
    SQLALCHEMY_DATABASE_URI = 'sqlite:////srv/redwind/test.db'
    # SQLALCHEMY_DATABASE_URI = 'postgres://redwind'

    # base url for pilbox requests (handles image proxy/resize)
    PILBOX_URL = '/imageproxy'

    # sign requests to prevent unauthorized use of pilbox service
    PILBOX_KEY = 'a key used to sign pilbox requests'

    # optionally, use Redis for async task queue. if this is disabled,
    # jobs will be stored in the database and qworker will poll for new
    # jobs.
    # REDIS_URL = 'redis://localhost:6379'
