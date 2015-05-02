from . import app
from . import auth
from . import contexts
from . import db
from . import hooks
from . import maps
from . import util
from .models import Post, Tag, Mention, Contact, Nick, Setting,\
    Venue, get_settings

from flask import request, redirect, url_for, render_template, flash, g,\
    abort, make_response, Markup, send_from_directory, session, current_app
import flask.ext.login as flask_login
from werkzeug import secure_filename

import sqlalchemy.orm
import sqlalchemy.sql
import sqlalchemy

import bs4
import collections
import datetime
import hashlib
import json
import mf2util
import operator
import os
import pytz
import re
import requests
import urllib.parse


TIMEZONE = pytz.timezone('US/Pacific')

POST_TYPES = [
    ('article', 'articles', 'All Articles'),
    ('note', 'notes', 'All Notes'),
    ('like', 'likes', 'All Likes'),
    ('share', 'shares', 'All Shares'),
    ('reply', 'replies', 'All Replies'),
    ('checkin', 'checkins', 'All Check-ins'),
    ('photo', 'photos', 'All Photos'),
    ('bookmark', 'bookmarks', 'All Bookmarks'),
]

POST_TYPE_RULE = '<any({}):post_type>'.format(','.join(tup[0] for tup in POST_TYPES))
PLURAL_TYPE_RULE = '<any({}):plural_type>'.format(','.join(tup[1] for tup in POST_TYPES))
DATE_RULE = ('<int:year>/<int(fixed_digits=2):month>/<int(fixed_digits=2):day>/<index>')
BEFORE_TS_FORMAT = '%Y%m%d%H%M%S'

AUTHOR_PLACEHOLDER = 'img/users/placeholder.png'


@app.context_processor
def inject_settings_variable():
    return {
        'settings': get_settings()
    }


def collect_posts(post_types, before_ts, per_page, tag, search=None,
                  include_hidden=False):
    query = Post.query
    query = query.options(
        sqlalchemy.orm.subqueryload(Post.tags),
        sqlalchemy.orm.subqueryload(Post.mentions),
        sqlalchemy.orm.subqueryload(Post.reply_contexts),
        sqlalchemy.orm.subqueryload(Post.repost_contexts),
        sqlalchemy.orm.subqueryload(Post.like_contexts),
        sqlalchemy.orm.subqueryload(Post.bookmark_contexts))
    if tag:
        query = query.filter(Post.tags.any(Tag.name == tag))
    if not include_hidden:
        query = query.filter_by(hidden=False)
    query = query.filter_by(deleted=False, draft=False)
    if post_types:
        query = query.filter(Post.post_type.in_(post_types))
    if search:
        query = query.filter(
            sqlalchemy.func.concat(Post.title, ' ', Post.content)
            .op('@@')(sqlalchemy.func.plainto_tsquery(search)))

    try:
        if before_ts:
            # convert ts in local timezone to utc and re-remove the timezone
            before_dt = datetime.datetime.strptime(before_ts, BEFORE_TS_FORMAT)\
                                         .replace(tzinfo=TIMEZONE)\
                                         .astimezone(datetime.timezone.utc)\
                                         .replace(tzinfo=None)
            query = query.filter(Post.published < before_dt)
    except ValueError:
        app.logger.warn('Could not parse before timestamp: %s', before_ts)

    query = query.order_by(Post.published.desc())
    query = query.limit(per_page)
    posts = query.all()

    posts = [post for post in posts if check_audience(post)]
    if posts:
        last_ts = posts[-1].published\
                           .replace(tzinfo=datetime.timezone.utc)\
                           .astimezone(TIMEZONE)\
                           .strftime(BEFORE_TS_FORMAT)
        view_args = request.view_args.copy()
        view_args['before_ts'] = last_ts
        for k, v in request.args.items():
            view_args[k] = v
        older = url_for(request.endpoint, **view_args)
    else:
        older = None

    return posts, older

# Font sizes in em. Maybe should be configurable
MIN_TAG_SIZE = 1.0
MAX_TAG_SIZE = 4.0
MIN_TAG_COUNT = 2


def render_tags(title, tags):
    counts = [tag['count'] for tag in tags]
    if counts:
        mincount, maxcount = min(counts), max(counts)
    for tag in tags:
        if maxcount > mincount:
            tag['size'] = (MIN_TAG_SIZE +
                           (MAX_TAG_SIZE - MIN_TAG_SIZE) *
                           (tag['count'] - mincount) /
                           (maxcount - mincount))
        else:
            tag['size'] = MIN_TAG_SIZE
    return util.render_themed('tags.jinja2', tags=tags, title=title,
                              max_tag_size=MAX_TAG_SIZE)


def render_posts(title, posts, older, template='posts.jinja2'):
    atom_args = request.view_args.copy()
    atom_args.update({'feed': 'atom', '_external': True})
    atom_url = url_for(request.endpoint, **atom_args)
    atom_title = title or 'Stream'
    return util.render_themed(template, posts=posts, title=title,
                              older=older, atom_url=atom_url,
                              atom_title=atom_title)


def render_posts_atom(title, feed_id, posts):
    return make_response(
        render_template('posts.atom', title=title, feed_id=feed_id,
                        posts=posts),
        200, {'Content-Type': 'application/atom+xml; charset=utf-8'})


@app.route('/')
@app.route('/before-<before_ts>')
def index(before_ts=None):
    posts, older = collect_posts(
        None, before_ts, int(get_settings().posts_per_page),
        None, include_hidden=False)
    if request.args.get('feed') == 'atom':
        return render_posts_atom('Stream', 'index.atom', posts)
    resp = make_response(
        render_posts('Stream', posts, older, template='home.jinja2'))

    if 'PUSH_HUB' in app.config:
        resp.headers.add('Link', '<{}>; rel="hub"'.format(
            app.config['PUSH_HUB']))
        resp.headers.add('Link', '<{}>; rel="self"'.format(
            url_for('index', _external=True)))
    return resp


@app.route('/everything')
@app.route('/everything/before-<before_ts>')
def everything(before_ts=None):
    posts, older = collect_posts(
        None, before_ts, int(get_settings().posts_per_page), None,
        include_hidden=True)

    if request.args.get('feed') == 'atom':
        return render_posts_atom('Everything', 'everything.atom', posts)
    return render_posts('Everything', posts, older)


@app.route('/' + PLURAL_TYPE_RULE)
@app.route('/' + PLURAL_TYPE_RULE + '/before-<before_ts>')
def posts_by_type(plural_type, before_ts=None):
    post_type, _, title = next(tup for tup in POST_TYPES
                               if tup[1] == plural_type)
    posts, older = collect_posts(
        (post_type,), before_ts, int(get_settings().posts_per_page), None,
        include_hidden=True)

    if request.args.get('feed') == 'atom':
        return render_posts_atom(title, plural_type + '.atom', posts)
    return render_posts(title, posts, older)


@app.route('/tags')
def tag_cloud():
    query = db.session.query(
        Tag.name, sqlalchemy.func.count(Post.id)
    ).join(Tag.posts)
    query = query.filter(sqlalchemy.sql.expression.not_(Post.deleted))
    if not flask_login.current_user.is_authenticated():
        query = query.filter(sqlalchemy.sql.expression.not_(Post.draft))
    query = query.group_by(Tag.id).order_by(Tag.name)
    query = query.having(sqlalchemy.func.count(Post.id) >= MIN_TAG_COUNT)
    tagdict = {}
    for name, count in query.all():
        tagdict[name] = tagdict.get(name, 0) + count
    tags = [
        {"name": name, "count": tagdict[name]}
        for name in sorted(tagdict)
    ]

    if tags:
        return render_tags("Tags", tags)
    else:
        flash('not enough tags, sorry!')
        return redirect('everything')


@app.route('/tags/<tag>')
@app.route('/tags/<tag>/before-<before_ts>')
def posts_by_tag(tag, before_ts=None):
    posts, older = collect_posts(
        None, before_ts, int(get_settings().posts_per_page), tag,
        include_hidden=True)
    title = '#' + tag

    if request.args.get('feed') == 'atom':
        return render_posts_atom(title, 'tag-' + tag + '.atom', posts)
    return render_posts(title, posts, older)


@app.route('/search')
@app.route('/search/before-<before_ts>')
def search(before_ts=None):
    q = request.args.get('q')
    if not q:
        abort(404)

    posts, older = collect_posts(
        None, before_ts, int(get_settings().posts_per_page), None,
        include_hidden=True, search=q)
    return render_posts('Search: ' + q, posts, older)


@app.route('/mentions')
def mentions():
    mentions = Mention.query.order_by(Mention.published.desc()).limit(100)
    return render_template('admin/mentions.jinja2', mentions=mentions)


@app.route('/all.atom')
def all_atom():
    return redirect(url_for('everything', feed='atom'))


@app.route('/updates.atom')
def updates_atom():
    return redirect(url_for('index', feed='atom'))


@app.route('/articles.atom')
def articles_atom():
    return redirect(url_for('posts_by_type', plural_type='articles', feed='atom'))


def check_audience(post):
    if not post.audience:
        # all posts public by default
        return True

    if flask_login.current_user.is_authenticated():
        # admin user can see everything
        return True

    if flask_login.current_user.is_anonymous():
        # anonymous users can't see stuff
        return False

    # check that their username is listed in the post's audience
    app.logger.debug('checking that logged in user %s is in post audience %s',
                     flask_login.current_user.get_id(), post.audience)
    return flask_login.current_user.get_id() in post.audience


@app.route('/<int:year>/<int(fixed_digits=2):month>/<slug>/files/<filename>')
def post_associated_file(year, month, slug, filename):
    post = Post.load_by_path('{}/{:02d}/{}'.format(year, month, slug))
    if not post:
        app.logger.debug('could not find post for path %s %s %s',
                         year, month, slug)
        abort(404)

    if post.deleted:
        abort(410)  # deleted permanently

    if not check_audience(post):
        abort(401)  # not authorized TODO a nicer page

    sourcepath = os.path.join(
        util.image_root_path(), '_data', post.path, 'files', filename)

    app.logger.debug('image source path: %s. request args: %s',
                     sourcepath, request.args)

    if not os.path.exists(sourcepath):
        app.logger.debug('source path does not exist %s', sourcepath)
        abort(404)

    if app.debug:
        _, ext = os.path.splitext(sourcepath)
        return send_from_directory(
            os.path.join(util.image_root_path(),
                         os.path.dirname(sourcepath)),
            os.path.basename(sourcepath), mimetype=None)

    resp = make_response('')
    # nginx is configured to serve internal resources directly
    sourcepath_internal = os.path.join(
        '/internal_data', post.path, 'files', filename)
    resp.headers['X-Accel-Redirect'] = sourcepath_internal
    del resp.headers['Content-Type']
    app.logger.debug('response with X-Accel-Redirect %s', resp.headers)
    return resp


@app.route('/' + POST_TYPE_RULE + '/' + DATE_RULE, defaults={'slug': None})
@app.route('/' + POST_TYPE_RULE + '/' + DATE_RULE + '/<slug>')
def post_by_date(post_type, year, month, day, index, slug):
    post = Post.load_by_historic_path('{}/{}/{:02d}/{:02d}/{}'.format(
        post_type, year, month, day, index))
    if not post:
        abort(404)
    return redirect(post.permalink)


@app.route('/<int:year>/<int(fixed_digits=2):month>/<slug>')
def post_by_path(year, month, slug):
    post = Post.load_by_path('{}/{:02d}/{}'.format(year, month, slug))
    return render_post(post)


@app.route('/drafts')
@flask_login.login_required
def all_drafts():
    posts = Post.query.filter_by(deleted=False, draft=True).all()
    return render_template('admin/drafts.jinja2', posts=posts)


@app.route('/drafts/<hash>')
def draft_by_hash(hash):
    post = Post.load_by_path('drafts/{}'.format(hash))
    return render_post(post)


def render_post(post):
    if not post:
        abort(404)

    if post.deleted:
        abort(410)  # deleted permanently

    if not check_audience(post):
        abort(401)  # not authorized TODO a nicer page

    if post.redirect:
        return redirect(post.redirect)

    return util.render_themed('post.jinja2', post=post, title=post.title_or_fallback)


def discover_endpoints(me):
    if me == get_settings().site_url:
        return (None, None, None)
    me_response = requests.get(me)
    if me_response.status_code != 200:
        excp = Exception("Unexpected response")
        excp.response = make_response(
            'Unexpected response from URL: {}'.format(me_response), 400)
        raise excp
    soup = bs4.BeautifulSoup(me_response.text)
    auth_endpoint = soup.find('link', {'rel': 'authorization_endpoint'})
    token_endpoint = soup.find('link', {'rel': 'token_endpoint'})
    micropub_endpoint = soup.find('link', {'rel': 'micropub'})

    return (auth_endpoint and auth_endpoint['href'],
            token_endpoint and token_endpoint['href'],
            micropub_endpoint and micropub_endpoint['href'])


@app.route('/login')
def login():
    me = request.args.get('me')
    if not me:
        return render_template('admin/login.jinja2',
                               next=request.args.get('next'))

    if app.config.get('BYPASS_INDIEAUTH'):
        user = auth.load_user(urllib.parse.urlparse(me).netloc)
        app.logger.debug('Logging in user %s', user)
        flask_login.login_user(user, remember=True)
        flash('logged in as {}'.format(me))
        app.logger.debug('Logged in with domain %s', me)
        return redirect(request.args.get('next') or url_for('index'))

    if not me:
        return make_response('Missing "me" parameter', 400)
    if not me.startswith('http://') and not me.startswith('https://'):
        me = 'http://' + me
    auth_url, token_url, micropub_url = discover_endpoints(me)
    if not auth_url:
        auth_url = 'https://indieauth.com/auth'

    app.logger.debug('Found endpoints %s, %s, %s', auth_url, token_url,
                     micropub_url)
    state = request.args.get('next')
    session['endpoints'] = (auth_url, token_url, micropub_url)

    auth_params = {
        'me': me,
        'client_id': get_settings().site_url,
        'redirect_uri': url_for('login_callback', _external=True),
        'state': state,
    }

    # if they support micropub try to get read indie-config permission
    if token_url and micropub_url:
        auth_params['scope'] = 'config'

    return redirect('{}?{}'.format(
        auth_url, urllib.parse.urlencode(auth_params)))


@app.route('/login_callback')
def login_callback():
    app.logger.debug('callback fields: %s', request.args)

    state = request.args.get('state')
    next_url = state or url_for('index')
    auth_url, token_url, micropub_url = session['endpoints']

    if not auth_url:
        flash('Login failed: No authorization URL in session')
        return redirect(next_url)

    code = request.args.get('code')
    client_id = get_settings().site_url
    redirect_uri = url_for('login_callback', _external=True)

    app.logger.debug('callback with auth endpoint %s', auth_url)
    response = requests.post(auth_url, data={
        'code': code,
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'state': state,
    })

    rdata = urllib.parse.parse_qs(response.text)
    if response.status_code != 200:
        app.logger.debug('call to auth endpoint failed %s', response)
        flash('Login failed {}: {}'.format(rdata.get('error'),
                                           rdata.get('error_description')))
        return redirect(next_url)

    app.logger.debug('verify response %s', response.text)
    if 'me' not in rdata:
        app.logger.debug('Verify response missing required "me" field')
        flash('Verify response missing required "me" field {}'.format(
            response.text))
        return redirect(next_url)

    me = rdata.get('me')[0]
    scopes = rdata.get('scope')
    user = auth.load_user(urllib.parse.urlparse(me).netloc)
    if not user:
        flash('No user for domain {}'.format(me))
        return redirect(next_url)

    try_micropub_config(token_url, micropub_url, scopes, code, me,
                        redirect_uri, client_id, state)

    app.logger.debug('Logging in user %s', user)
    flask_login.login_user(user, remember=True)
    flash('Logged in with domain {}'.format(me))
    app.logger.debug('Logged in with domain %s', me)

    return redirect(next_url)


def try_micropub_config(token_url, micropub_url, scopes, code, me,
                        redirect_uri, client_id, state):
    if not scopes or 'config' not in scopes or not token_url or not micropub_url:
        flash('Micropub not supported (which is fine).')
        return False

    token_response = requests.post(token_url, data={
        'code': code,
        'me': me,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'state': state,
    })

    if token_response.status_code != 200:
        flash('Unexpected response from token endpoint {}'.format(
            token_response))
        return False

    tdata = urllib.parse.parse_qs(token_response.text)
    if 'access_token' not in tdata:
        flash('Response from token endpoint missing '
              'access_token {}'.format(tdata))
        return False

    access_token = tdata.get('access_token')[0]
    session['micropub'] = (micropub_url, access_token)

    flash('Got micropub access token {}'.format(access_token[:6] + '...'))

    actions_response = requests.get(micropub_url + '?q=actions', headers={
        'Authorization': 'Bearer ' + access_token,
    })
    if actions_response.status_code != 200:
        app.logger.debug(
            'Bad response to action handler query %s', actions_response)
        return False

    app.logger.debug('Successful action handler query %s',
                     actions_response.text)
    actions_content_type = actions_response.headers.get('content-type', '')
    if 'application/json' in actions_content_type:
        adata = json.loads(actions_response.text)
        app.logger.debug('action handlers (json): %s', adata)
        session['action-handlers'] = adata
    else:
        adata = urllib.parse.parse_qs(actions_response.text)
        app.logger.debug('action handlers: %s', adata)
        session['action-handlers'] = {
            key: value[0] for key, value in adata.items()}
    return True


@app.route('/logout')
def logout():
    flask_login.logout_user()
    for key in ('action-handlers', 'endpoints', 'micropub'):
        if key in session:
            del session[key]
    return redirect(request.args.get('next', url_for('index')))


@app.route('/settings', methods=['GET', 'POST'])
@flask_login.login_required
def edit_settings():
    if request.method == 'GET':
        return render_template('admin/settings.jinja2', raw_settings=sorted(
            Setting.query.all(), key=operator.attrgetter('name')))
    for key, value in request.form.items():
        Setting.query.get(key).value = value
    db.session.commit()

    return redirect(url_for('edit_settings'))


@app.route('/delete')
@flask_login.login_required
def delete_by_id():
    id = request.args.get('id')
    post = Post.load_by_id(id)
    if not post:
        abort(404)
    post.deleted = True
    db.session.commit()

    redirect_url = request.args.get('redirect') or url_for('index')
    app.logger.debug('redirecting to {}'.format(redirect_url))
    return redirect(redirect_url)


def get_tags():
    return [t.name for t in Tag.query.all()]


def get_top_tags(n=10):
    """
    Determine top-n tags based on a combination of frequency and receny.
    ref: https://developer.mozilla.org/en-US/docs/Mozilla/Tech/Places/Frecency_algorithm
    """
    rank = collections.defaultdict(int)
    now = datetime.datetime.utcnow()

    entries = Post.query.join(Post.tags).values(Post.published, Tag.name)
    for published, tag in entries:
        weight = 0
        if published:
            if published.tzinfo:
                published = published.astimezone(datetime.timezone.utc)
                published = published.replace(tzinfo=None)
            delta = now - published
            if delta < datetime.timedelta(days=4):
                weight = 1.0
            elif delta < datetime.timedelta(days=14):
                weight = 0.7
            elif delta < datetime.timedelta(days=31):
                weight = 0.5
            elif delta < datetime.timedelta(days=90):
                weight = 0.3
            elif delta < datetime.timedelta(days=730):
                weight = 0.1
        rank[tag] += weight

    ordered = sorted(list(rank.items()), key=operator.itemgetter(1),
                     reverse=True)
    return [key for key, _ in ordered[:n]]


@app.route('/new/<type>')
@app.route('/new', defaults={'type': 'note'})
def new_post(type):
    post = Post(type)
    post.published = datetime.datetime.utcnow()
    post.content = ''

    if type == 'reply':
        in_reply_to = request.args.get('url')
        if in_reply_to:
            post.in_reply_to = [in_reply_to]
            # post.reply_contexts = [contexts.create_context(in_reply_to)]

    elif type == 'share':
        repost_of = request.args.get('url')
        if repost_of:
            post.repost_of = [repost_of]
            # post.repost_contexts = [contexts.create_context(repost_of)]

    elif type == 'like':
        like_of = request.args.get('url')
        if like_of:
            post.like_of = [like_of]
            # post.like_contexts = [contexts.create_context(like_of)]

    elif type == 'bookmark':
        bookmark_of = request.args.get('url')
        if bookmark_of:
            post.bookmark_of = [bookmark_of]
            # post.bookmark_contexts = [contexts.create_context(bookmark_of)]

    post.content = request.args.get('content')
    button_text = {
        'publish': 'Publish',
        'publish_quietly': 'Publish Quietly',
        'publish+tweet': 'Publish & Tweet',
        'save_draft': 'Save as Draft',
    }

    return render_template('admin/edit_' + type + '.jinja2',
                           edit_type='new', post=post,
                           tags=get_tags(), top_tags=get_top_tags(20),
                           button_text=button_text)


@app.route('/edit')
def edit_by_id():
    id = request.args.get('id')
    post = Post.load_by_id(id)
    if not post:
        abort(404)
    type = 'post'
    if not request.args.get('advanced') and post.post_type:
        type = post.post_type

    if post.draft:
        button_text = {
            'publish': 'Publish Draft',
            'publish_quietly': 'Publish Draft Quietly',
            'publish+tweet': 'Publish Draft & Tweet',
            'save_draft': 'Resave Draft',
        }
    else:
        button_text = {
            'publish': 'Republish',
            'publish_quietly': 'Republish Quietly',
            'publish+tweet': 'Republish & Tweet',
            'save_draft': 'Unpublish, Save as Draft',
        }

    template = 'admin/edit_' + type + '.jinja2'
    if request.args.get('full'):
        template = 'admin/edit_post_all.jinja2'

    return render_template(template, edit_type='edit', post=post,
                           tags=get_tags(), top_tags=get_top_tags(20),
                           button_text=button_text)


@app.route('/uploads')
def uploads_popup():
    return render_template('uploads_popup.jinja2')


@app.template_filter('json')
def to_json(obj):
    return Markup(json.dumps(obj))


@app.template_filter('approximate_latitude')
def approximate_latitude(loc):
    latitude = loc.get('latitude')
    if latitude:
        return '{:.3f}'.format(latitude)


@app.template_filter('approximate_longitude')
def approximate_longitude(loc):
    longitude = loc.get('longitude')
    return longitude and '{:.3f}'.format(longitude)


@app.template_filter('geo_name')
def geo_name(loc):
    name = loc.get('name')
    if name:
        return name

    locality = loc.get('locality')
    region = loc.get('region')
    if locality and region:
        return "{}, {}".format(locality, region)

    latitude = loc.get('latitude')
    longitude = loc.get('longitude')
    if latitude and longitude:
        return "{:.2f}, {:.2f}".format(float(latitude), float(longitude))

    return "Unknown Location"


@app.template_filter('isotime')
def isotime_filter(thedate):
    if thedate:
        thedate = thedate.replace(microsecond=0)
        if hasattr(thedate, 'tzinfo') and not thedate.tzinfo:
            tz = pytz.timezone(get_settings().timezone)
            thedate = pytz.utc.localize(thedate).astimezone(tz)
        if isinstance(thedate, datetime.datetime):
            return thedate.isoformat('T')
        return thedate.isoformat()


@app.template_filter('human_time')
def human_time(thedate, alternate=None):
    if not thedate:
        return alternate

    if hasattr(thedate, 'tzinfo') and not thedate.tzinfo:
        tz = pytz.timezone(get_settings().timezone)
        thedate = pytz.utc.localize(thedate).astimezone(tz)

    if (isinstance(thedate, datetime.datetime)
            and datetime.datetime.now(TIMEZONE) - thedate < datetime.timedelta(days=1)):
        return thedate.strftime('%B %-d, %Y %-I:%M%P %Z')
    else:
        return thedate.strftime('%B %-d, %Y')


@app.template_filter('date')
def date_filter(thedate, first_only=False):
    if thedate:
        if hasattr(thedate, 'tzinfo') and not thedate.tzinfo:
            tz = pytz.timezone(get_settings().timezone)
            thedate = pytz.utc.localize(thedate).astimezone(tz)

        previous = getattr(g, 'previous date', None)
        formatted = thedate.strftime('%B %-d, %Y')
        setattr(g, 'previous date', formatted)
        if not first_only or not previous or previous != formatted:
            return formatted


@app.template_filter('time')
def time_filter(thedate):
    if thedate:
        if hasattr(thedate, 'tzinfo') and not thedate.tzinfo:
            tz = pytz.timezone(get_settings().timezone)
            thedate = pytz.utc.localize(thedate).astimezone(tz)
        if isinstance(thedate, datetime.datetime):
            return thedate.strftime('%-I:%M%P %Z')


@app.template_filter('pluralize')
def pluralize(number, singular='', plural='s'):
    if number == 1:
        return singular
    else:
        return plural


@app.template_filter('month_shortname')
def month_shortname(month):
    return datetime.date(1990, month, 1).strftime('%b')


@app.template_filter('month_name')
def month_name(month):
    return datetime.date(1990, month, 1).strftime('%B')


@app.template_filter('atom_sanitize')
def atom_sanitize(content):
    return Markup.escape(str(content))


@app.template_filter('prettify_url')
def prettify_url(*args, **kwargs):
    return util.prettify_url(*args, **kwargs)


@app.template_filter('domain_from_url')
def domain_from_url(url):
    if not url:
        return url
    return urllib.parse.urlparse(url).netloc


@app.template_filter('format_syndication_url')
def format_syndication_url(url, include_rel=True):
    fmt = '<a class="u-syndication" '
    if include_rel:
        fmt += 'rel="syndication" '
    fmt += 'href="{}"><i class="fa {}"></i> {}</a>'

    if util.TWITTER_RE.match(url):
        return Markup(fmt.format(url, 'fa-twitter', 'Twitter'))
    if util.FACEBOOK_RE.match(url):
        return Markup(fmt.format(url, 'fa-facebook', 'Facebook'))
    if util.INSTAGRAM_RE.match(url):
        return Markup(fmt.format(url, 'fa-instagram', 'Instagram'))

    return Markup(fmt.format(url, 'fa-paper-plane', domain_from_url(url)))


IMAGE_TAG_RE = re.compile(r'<img([^>]*) src="(https?://[^">]+)"')


@app.template_filter('proxy_all')
def proxy_all_images(html, side=None):
    def repl(m):
        url = m.group(2)
        # don't proxy images that come from this site
        if url.startswith(get_settings().site_url):
            return m.group(0)
        return '<img{} src="{}"'.format(m.group(1), imageproxy(m.group(2), side=side))
    return IMAGE_TAG_RE.sub(repl, html) if html else html


@app.template_filter('imageproxy')
def imageproxy(src, side=None):
    parsed = urllib.parse.urlparse(src)
    if parsed.netloc and parsed.netloc.startswith('localhost'):
        return src
    return util.construct_imageproxy_url(src, side)


@app.route('/save_edit', methods=['POST'])
@flask_login.login_required
def save_edit():
    id = request.form.get('post_id')
    app.logger.debug('saving post %s', id)
    post = Post.load_by_id(id)
    return save_post(post)


@app.route('/save_new', methods=['POST'])
@flask_login.login_required
def save_new():
    post_type = request.form.get('post_type', 'note')
    app.logger.debug('saving new post of type %s', post_type)
    post = Post(post_type)
    return save_post(post)


def save_post(post):
    was_draft = post.draft

    pub_str = request.form.get('published')
    if pub_str:
        pub = mf2util.parse_dt(pub_str)
        if pub.tzinfo:
            pub = pub.astimezone(datetime.timezone.utc)
            pub = pub.replace(tzinfo=None)
        post.published = pub

    if not post.published or was_draft:
        post.published = datetime.datetime.utcnow()

    # populate the Post object and save it to the database,
    # redirect to the view
    post.title = request.form.get('title', '')
    post.content = request.form.get('content')
    post.draft = request.form.get('action') == 'save_draft'
    post.hidden = request.form.get('hidden', 'false') == 'true'

    venue_name = request.form.get('new_venue_name')
    venue_lat = request.form.get('new_venue_latitude')
    venue_lng = request.form.get('new_venue_longitude')
    if venue_name and venue_lat and venue_lng:
        venue = Venue()
        venue.name = venue_name
        venue.location = {
            'latitude': float(venue_lat),
            'longitude': float(venue_lng),
        }
        venue.update_slug('{}-{}'.format(venue_lat, venue_lng))
        db.session.add(venue)
        db.session.commit()
        hooks.fire('venue-saved', venue, request.form)
        post.venue = venue

    else:
        venue_id = request.form.get('venue')
        if venue_id:
            post.venue = Venue.query.get(venue_id)

    lat = request.form.get('latitude')
    lon = request.form.get('longitude')
    if lat and lon:
        if post.location is None:
            post.location = {}

        post.location['latitude'] = float(lat)
        post.location['longitude'] = float(lon)
        loc_name = request.form.get('location_name')
        if loc_name is not None:
            post.location['name'] = loc_name
    else:
        post.location = None

    for url_attr, context_attr in (('in_reply_to', 'reply_contexts'),
                                   ('repost_of', 'repost_contexts'),
                                   ('like_of', 'like_contexts'),
                                   ('bookmark_of', 'bookmark_contexts')):
        url_str = request.form.get(url_attr)
        if url_str is not None:
            urls = util.multiline_string_to_list(url_str)
            setattr(post, url_attr, urls)

    # fetch contexts before generating a slug
    contexts.fetch_contexts(post)

    syndication = request.form.get('syndication')
    if syndication is not None:
        post.syndication = util.multiline_string_to_list(syndication)

    audience = request.form.get('audience')
    if audience is not None:
        post.audience = util.multiline_string_to_list(audience)

    tags = request.form.getlist('tags')
    tags = list(filter(None, map(util.normalize_tag, tags)))
    post.tags = [Tag.query.filter_by(name=tag).first() or Tag(tag)
                 for tag in tags]

    slug = request.form.get('slug')
    if slug:
        post.slug = util.slugify(slug)
    elif not post.slug or was_draft:
        post.slug = post.generate_slug()

    if post.draft:
        m = hashlib.md5()
        m.update(bytes(post.published.isoformat() + '|' + post.slug,
                       'utf-8'))
        post.path = 'drafts/{}'.format(m.hexdigest())

    elif not post.path or was_draft:
        base_path = '{}/{:02d}/{}'.format(
            post.published.year, post.published.month, post.slug)
        # generate a unique path
        unique_path = base_path
        idx = 1
        while Post.load_by_path(unique_path):
            unique_path = '{}-{}'.format(base_path, idx)
            idx += 1
        post.path = unique_path

    # TODO accept multiple photos and captions
    inphoto = request.files.get('photo')
    if inphoto and inphoto.filename:
        app.logger.debug('receiving uploaded file %s', inphoto)
        relpath, photo_url, fullpath \
            = generate_upload_path(post, inphoto)
        if not os.path.exists(os.path.dirname(fullpath)):
            os.makedirs(os.path.dirname(fullpath))
        app.logger.debug('uploading photo to %s', fullpath)
        inphoto.save(fullpath)
        caption = request.form.get('caption')
        post.photos = [{
            'filename': os.path.basename(relpath),
            'caption': caption,
        }]

    file_to_url = {}
    infiles = request.files.getlist('files')
    app.logger.debug('infiles: %s', infiles)
    for infile in infiles:
        if infile and infile.filename:
            app.logger.debug('receiving uploaded file %s', infile)
            relpath, photo_url, fullpath \
                = generate_upload_path(post, infile)
            if not os.path.exists(os.path.dirname(fullpath)):
                os.makedirs(os.path.dirname(fullpath))
            infile.save(fullpath)
            file_to_url[infile] = photo_url

    app.logger.debug('uploaded files map %s', file_to_url)

    # pre-render the post html
    post.content_html = util.markdown_filter(
        post.content, img_path=post.get_image_path(),
        person_processor=util.person_to_microcard if post.post_type == 'article'
        else util.person_to_at_name)

    if not post.id:
        db.session.add(post)
    db.session.commit()

    app.logger.debug('saved post %d %s', post.id, post.permalink)
    redirect_url = post.permalink

    hooks.fire('post-saved', post, request.form)

    return redirect(redirect_url)


def generate_upload_path(post, f, default_ext=None):
    filename = secure_filename(f.filename)
    basename, ext = os.path.splitext(filename)

    if ext:
        app.logger.debug('file has extension: %s, %s', basename, ext)
    else:
        app.logger.debug('no file extension, checking mime_type: %s',
                         f.mimetype)
        if f.mimetype == 'image/png':
            ext = '.png'
        elif f.mimetype == 'image/jpeg':
            ext = '.jpg'
        elif default_ext:
            # fallback application/octet-stream
            ext = default_ext

    # special handling for ugly filenames from OwnYourGram
    if basename.startswith('tmp_') and ext.lower() in ('.png', '.jpg'):
        basename = 'photo'

    idx = 0
    while True:
        if idx == 0:
            filename = '{}{}'.format(basename, ext)
        else:
            filename = '{}-{}{}'.format(basename, idx, ext)
        relpath = '{}/files/{}'.format(post.path, filename)
        fullpath = os.path.join(util.image_root_path(), '_data', relpath)
        if not os.path.exists(fullpath):
            break
        idx += 1

    return relpath, '/' + relpath, fullpath


@app.route('/addressbook')
def addressbook():
    return redirect(url_for('contacts'))


@app.route('/contacts')
def contacts():
    contacts = Contact.query.order_by(Contact.name).all()
    return render_template('admin/contacts.jinja2', contacts=contacts)


@app.route('/contacts/<name>')
def contact_by_name(name):
    nick = Nick.query.filter_by(name=name).first()
    contact = nick and nick.contact
    if not contact:
        abort(404)
    return render_template('admin/contact.jinja2', contact=contact)


@app.route('/delete/contact')
@flask_login.login_required
def delete_contact():
    id = request.args.get('id')
    contact = Contact.query.get(id)
    db.session.delete(contact)
    db.session.commit()
    return redirect(url_for('contacts'))


@app.route('/new/contact', methods=['GET', 'POST'])
def new_contact():
    if request.method == 'GET':
        contact = Contact()
        return render_template('admin/edit_contact.jinja2', contact=contact)

    if not flask_login.current_user.is_authenticated():
        return current_app.login_manager.unauthorized()

    contact = Contact()
    db.session.add(contact)
    return save_contact(contact)


@app.route('/edit/contact', methods=['GET', 'POST'])
def edit_contact():
    if request.method == 'GET':
        id = request.args.get('id')
        contact = Contact.query.get(id)
        return render_template('admin/edit_contact.jinja2', contact=contact)

    if not flask_login.current_user.is_authenticated():
        return current_app.login_manager.unauthorized()

    id = request.form.get('id')
    contact = Contact.query.get(id)
    return save_contact(contact)


def save_contact(contact):
    contact.name = request.form.get('name')
    contact.image = request.form.get('image')
    contact.url = request.form.get('url')

    for nick in contact.nicks:
        db.session.delete(nick)
    db.session.commit()

    contact.nicks = [Nick(name=nick.strip())
                     for nick
                     in request.form.get('nicks', '').split(',')
                     if nick.strip()]

    contact.social = util.filter_empty_keys({
        'twitter': request.form.get('twitter'),
        'facebook': request.form.get('facebook'),
    })

    if not contact.id:
        db.session.add(contact)
    db.session.commit()

    if contact.nicks:
        return redirect(url_for('contact_by_name', name=contact.nicks[0].name))
    else:
        return redirect(url_for('contacts'))


@app.route('/venues/<slug>')
def venue_by_slug(slug):
    venue = Venue.query.filter_by(slug=slug).first()
    if not venue:
        abort(404)
    app.logger.debug('rendering venue, location. %s, %s',
                     venue, venue.location)
    posts = Post.query.filter_by(venue_id=venue.id).all()
    return render_template('admin/venue.jinja2', venue=venue, posts=posts)


@app.route('/venues')
def all_venues():
    venues = Venue.query.order_by(Venue.name).all()
    markers = [maps.Marker(v.location.get('latitude'),
                           v.location.get('longitude'),
                           'dot-small-pink')
               for v in venues]

    organized = {}
    for venue in venues:
        region = venue.location.get('region')
        locality = venue.location.get('locality')
        if region and locality:
            organized.setdefault(region, {})\
                     .setdefault(locality, [])\
                     .append(venue)

    map_image = maps.get_map_image(600, 400, 13, markers)
    return render_template('admin/venues.jinja2', venues=venues,
                           organized=organized, map_image=map_image)


@app.route('/new/venue', methods=['GET', 'POST'])
def new_venue():
    venue = Venue()
    if request.method == 'GET':
        return render_template('admin/edit_venue.jinja2', venue=venue)
    return save_venue(venue)


@app.route('/edit/venue', methods=['GET', 'POST'])
def edit_venue():
    id = request.args.get('id')
    venue = Venue.query.get(id)
    if request.method == 'GET':
        return render_template('admin/edit_venue.jinja2', venue=venue)
    return save_venue(venue)


@app.route('/delete/venue')
@flask_login.login_required
def delete_venue():
    id = request.args.get('id')
    venue = Venue.query.get(id)
    db.session.delete(venue)
    db.session.commit()
    return redirect(url_for('all_venues'))


def save_venue(venue):
    venue.name = request.form.get('name')
    venue.location = {
        'latitude': float(request.form.get('latitude')),
        'longitude': float(request.form.get('longitude')),
    }
    venue.update_slug(request.form.get('geocode'))

    if not venue.id:
        db.session.add(venue)
    db.session.commit()

    hooks.fire('venue-saved', venue, request.form)
    return redirect(url_for('venue_by_slug', slug=venue.slug))
