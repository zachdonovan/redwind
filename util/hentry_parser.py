# Copyright © 2013, 2014 Kyle Mahan
# This file is part of Red Wind.
#
# Red Wind is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Red Wind is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Red Wind.  If not, see <http://www.gnu.org/licenses/>.


from . import mf2
from dateutil.parser import parse as parsedate
import pytz


class Author:
    def __init__(self, name, url, photo):
        self.name = name
        self.url = url
        self.photo = photo

    def __repr__(self):
        return "Author[name={}, url={}, photo={}]".format(
            self.name, self.url, self.photo)


class Reference:
    def __init__(self, url, reftype):
        self.url = url
        self.reftype = reftype

    def __repr__(self):
        return "Reference[url={}, reftype={}]".format(self.url, self.reftype)


class Entry:
    def __init__(self, author, permalink, pub_date,
                 references, title, content):
        self.author = author
        self.permalink = permalink
        self.pub_date = pub_date
        self.references = references
        self.title = title
        self.content = content

    def __repr__(self):
        return "Entry[author={}, permalink={}, pub_date={}, references={}, title={}, content={}]".format(
            self.author, self.permalink, self.pub_date,
            self.references, self.title, self.content[:100].replace('\n', ''))


def parse(txt, source):
    def parse_references(objs, reftype):
        refs = []
        for obj in objs:
            if isinstance(obj, str):
                refs.append(Reference(obj, reftype))
            else:
                refs += [Reference(url, reftype) for url
                         in obj.get('properties', {}).get('url', [])]
        return refs

    def parse_author(obj):
        if isinstance(obj, str):
            return Author(obj, None, None)
        else:
            names = obj['properties'].get('name')
            photos = obj['properties'].get('photo')
            urls = obj['properties'].get('url')
            return Author(names and names[0],
                          urls and urls[0],
                          photos and photos[0])

    #p = Parser(doc=txt, url=source)
    #d = p.to_dict()
    d = mf2.parse(txt, source)
    references = []

    for rel, rel_url in d['rels'].items():
        if rel in ('like', 'like-of'):
            references.append(Reference(rel_url, 'like'))
        elif rel in ('reply', 'reply-to', 'in-reply-to'):
            references.append(Reference(rel_url, 'reply'))
        elif rel in ('repost', 'repost-of'):
            references.append(Reference(rel_url, 'repost'))

    entry = None
    for item in d['items']:
        if 'h-entry' in item['type']:

            hentry = item
            permalink = next((perma for perma
                              in hentry['properties'].get('url', [])), source)

            references += parse_references(
                hentry['properties'].get('in-reply-to', []), 'reply')

            references += parse_references(
                hentry['properties'].get('like-of', []), 'like')

            references += parse_references(
                hentry['properties'].get('repost-of', []), 'repost')

            date_strs = hentry['properties'].get('published')
            pub_date = date_strs and parsedate(' '.join(date_strs))
            if pub_date and pub_date.tzinfo:
                pub_date = pub_date.astimezone(pytz.utc)

            content_html = ''.join(content['html'].strip() for content
                                   in hentry['properties'].get('content', []))
            content_value = ''.join(content['value'].strip() for content
                                    in hentry['properties'].get('content', []))

            title = ''.join(part.strip() for part
                            in hentry['properties'].get('name', ''))

            if title == content_value:
                title = None

            author = None
            for obj in hentry['properties'].get('author', []):
                author = parse_author(obj)
                break

            entry = Entry(author, permalink, pub_date, references, title,
                          content_html)
            break

    hcards = [item for item in d['items'] if 'h-card' in item['type']]

    if entry and not entry.author:
        for item in hcards:
            urls = item['properties'].get('url', [])
            if source in urls:
                entry.author = parse_author(item)
                break

    if entry and not entry.author:
        rel_mes = d["rels"].get("me", [])
        for item in hcards:
            urls = item['properties'].get('url', [])
            if any(url in rel_mes for url in urls):
                entry.author = parse_author(item)
                break

    if entry and not entry.author:
        for item in hcards:
            if 'url' in item['properties']:
                entry.author = parse_author(item)
                break

    if entry and not entry.author and hcards:
        entry.author = parse_author(hcards[0])

    return entry


if __name__ == '__main__':
    import requests
    urls = [
        'https://snarfed.org/2014-03-10_re-kyle-mahan',
        'https://brid-gy.appspot.com/like/facebook/12802152/10100820912531629/1347771058',
        'http://tantek.com/2014/030/t1/handmade-art-indieweb-reply-webmention-want',
        'http://tantek.com/2014/067/b2/mockups-people-focused-mobile-communication',
        'https://brid-gy.appspot.com/comment/twitter/kyle_wm/443763597160636417/443787536108761088',
        'https://snarfed.org/2014-03-10_re-kyle-mahan-5',
        'http://tommorris.org/posts/2550'
    ]

    for url in urls:
        print("parsing url", url)
        txt = requests.get(url).text
        print(parse(txt, url))
        print()
