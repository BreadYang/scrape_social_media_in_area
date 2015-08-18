#!/usr/bin/env python

# Every so often (TBD), takes a food, messes its name up, grabs an image of
# that food, and makes a meme with the misspelled word, posts it to Twitter.

import argparse, random, ConfigParser, os, time, datetime, ppygis, pytz, traceback
import psycopg2, psycopg2.extensions
from collections import defaultdict
from twython import TwythonStreamer
import twython.exceptions

config = ConfigParser.ConfigParser()
config.read('config.txt')

OAUTH_KEYS = [{'consumer_key': config.get('twitter-' + str(i), 'consumer_key'),
              'consumer_secret': config.get('twitter-' + str(i), 'consumer_secret'),
              'access_token_key': config.get('twitter-' + str(i), 'access_token_key'),
              'access_token_secret': config.get('twitter-' + str(i), 'access_token_secret')}
             for i in range(int(config.get('num_twitter_credentials', 'num')))]

CITY_LOCATIONS = {
    'pgh':  { 'locations': '-80.2,40.241667,-79.8,40.641667' },
    'sf':   { 'locations': '-122.5950,37.565,-122.295,37.865' },
    'ny':   { 'locations': '-74.03095193,40.6815699768,-73.9130315074,40.8343765254' },
    'houston': { 'locations': '-95.592778, 29.550556, -95.138056, 29.958333' },
    'detroit': { 'locations': '-83.2458, 42.1314, -82.8458, 42.5314' },
    'chicago': { 'locations': '-87.9847, 41.6369, -87.5847, 42.0369' },
    'cleveland': { 'locations': '-81.9697, 41.1822, -81.4697, 41.5822' },
    'seattle': { 'locations': '-122.5331, 47.4097, -121.9331, 47.8097' },
    'miami': { 'locations': '-80.4241, 25.5877, -80.0641, 26.2877' },
    'london': { 'locations': '-0.4275, 51.3072, 0.2525, 51.7072' },
    'minneapolis': { 'locations': '-93.465, 44.7778, -93.065, 45.1778' },
    'austin': {'locations': '-97.95, 30.05, -97.55, 30.45'},
    'sanantonio': {'locations': '-98.7, 29.21667, -98.3, 29.61667'},
    'dallas': {'locations': '-96.996667, 32.575833, -96.596667, 32.975833'},
    'whitehouse': {'locations': '-77.038, 38.8965, -77.035, 38.8985'},
}

month_map = {'Jan': 1, 'Feb': 2, 'Mar':3, 'Apr':4, 'May':5, 'Jun':6, 'Jul':7, 
    'Aug':8,  'Sep': 9, 'Oct':10, 'Nov': 11, 'Dec': 12}
# Special case date parsing. All our dates are like this:
# Wed Jan 22 23:19:19 +0000 2014
# 012345678901234567890123456789
# so let's just parse them like that. 
def parse_date(d):
    return datetime.datetime(int(d[26:30]), month_map[d[4:7]], int(d[8:10]),\
        int(d[11:13]), int(d[14:16]), int(d[17:19]), 0, pytz.timezone('UTC')) # 0=usec

# Argument: a tweet JSON object and a table string name to insert into.
# Returns: a string starting with "INSERT..." that you can run to insert this
# tweet into a Postgres database.
def tweet_to_insert_string(tweet, table):
    lat = tweet['coordinates']['coordinates'][1]
    lon = tweet['coordinates']['coordinates'][0]
    coordinates = ppygis.Point(lon, lat, srid=4326)
    created_at = parse_date(tweet['created_at'])
    hstore_user = make_hstore(tweet['user'])
    hstore_place = make_hstore(tweet['place'])
    hstore_entities = make_hstore(tweet['entities'])

    # Sometimes there's no lang, or filter_level. Not sure why. Fix it I guess?
    if 'filter_level' not in tweet:
        tweet['filter_level'] = ''
    if 'lang' not in tweet:
        tweet['lang'] = ''

    insert_str = pg_cur.mogrify("INSERT INTO " + table + "(contributors, " +
            "coordinates, created_at, entities, favorite_count, filter_level, " +
            "lang, id, id_str, in_reply_to_screen_name, in_reply_to_status_id, " +
            "in_reply_to_status_id_str, in_reply_to_user_id, in_reply_to_user_id_str, " +
            "place, retweet_count, source, twitter_user, text, user_screen_name) " +
            "VALUES (" + ','.join(['%s' for key in data_types]) + ")",
        (tweet['contributors'], coordinates, created_at, hstore_entities, tweet['favorite_count'],
        tweet['filter_level'], tweet['lang'], tweet['id'], tweet['id_str'],
        tweet['in_reply_to_screen_name'], tweet['in_reply_to_status_id'],
        tweet['in_reply_to_status_id_str'], tweet['in_reply_to_user_id'],
        tweet['in_reply_to_user_id_str'], hstore_place, tweet['retweet_count'],
        tweet['source'], hstore_user, tweet['text'],
        tweet['user']['screen_name']))
    return insert_str

# Map of field name -> postgres datatype. Contains everything we want to save.
# TODO: if you change this, also change the tweet_to_insert_string method below.
data_types = {
    # _id skipped; it's from mongodb. id (no _) works, and ensures no duplicate tweets.
    'contributors': 'text', # TODO does this work?
    'coordinates': 'Point',
    'created_at': 'timestamp',
    'entities': 'hstore', 
    'favorite_count': 'integer',
    # |favorited| only makes sense if there's an authenticated user.
    'filter_level': 'text',
    # geo skipped, b/c it's deprecated
    'lang': 'text',
    'id': 'bigint primary key',
    'id_str': 'text',
    'in_reply_to_screen_name': 'text',
    'in_reply_to_status_id': 'bigint',
    'in_reply_to_status_id_str': 'text',
    'in_reply_to_user_id': 'bigint',
    'in_reply_to_user_id_str': 'text',
    'place': 'hstore',
    'retweet_count': 'integer',
    # |retweeted| only make sense if there's an authenticated user.
    'source': 'text',
    'text': 'text NOT NULL',
    # |truncated| is obsolete; Twitter now rejects long tweets instead of truncating.
    'twitter_user': 'hstore', # was |user| in Twitter API.
    'user_screen_name': 'text NOT NULL', # added this
}

# Argument: a python dictionary. Returns: the same thing with all keys and
# values as strings, so we can make a postgres hstore with them.
def make_hstore(py_dict):
    if not py_dict:
        py_dict={}
    return {unicode(k): unicode(v) for k, v in py_dict.iteritems()}

class MyStreamer(TwythonStreamer):
    psql_connection = None
    psql_cursor = None
    psql_table = None
    buffer = ''
 
    def __init__(psql_conn, table_name):
        self.psql_connection = psql_conn
        self.psql_cursor = self.psql_connection.cursor()
        self.psql_table = table_name
        psycopg2.extras.register_hstore(self.psql_connection)

    def on_success(self, data):
        print data
        if 'text' in data:
            print data['text'].encode('utf-8')
        self.buffer += data
        if data.endswith('\r\n') and self.buffer.strip():
            # complete message received
            message = json.loads(self.buffer)
            self.buffer = ''
            msg = ''
            if message.get('limit'):
                log('Rate limiting caused us to miss %s tweets' % (message['limit'].get('track')))
            elif message.get('disconnect'):
                raise Exception('Got disconnect: %s' % message['disconnect'].get('reason'))
            elif message.get('warning'):
                log('Got warning: %s' % message['warning'].get('message'))
            elif message['coordinates'] == None:
                pass # message with no actual coordinates, just a bounding box
            else:
                lon = message['coordinates']['coordinates'][0]
                lat = message['coordinates']['coordinates'][1]
                if lon >= self.min_lon and lon <= self.max_lon and \
                        lat >= self.min_lat and lat <= self.max_lat:
                    # db[self.tweet_col].insert(dict(message))
                    self.save_to_postgres(dict(message))
                    log('Got tweet with text: %s' % message.get('text').encode('utf-8'))
                    # TODO save foursquare data to its own table
                    # self.save_foursquare_data_if_present(message)


    def on_error(self, status_code, data):
        print status_code
        print data

    # Given a tweet from the Twitter API, saves it to Postgres DB table |table|.
    def save_to_postgres(self, tweet):
        insert_str = tweet_to_insert_string(tweet, self.psql_table)
        try:
            self.pg_cur.execute(insert_str)
            self.psql_conn.commit()
       
        except Exception as e:
            print "Error running this command: %s" % insert_str
            traceback.print_exc()
            traceback.print_stack()
            self.psql_conn.rollback() # just ignore the error
            # because, whatever, we miss one tweet


def post_tweet(image, fud):
    # TODO update this to use upload_media and then a separate post instead.
    twitter.update_status_with_media(media=image_io, status='')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--foods_file', default='foods.txt')
    args = parser.parse_args()

    psql_conn = psycopg2.connect("dbname='tweet'")
    psycopg2.extras.register_hstore(psql_conn)
    pg_cur = psql_conn.cursor()

    keys = OAUTH_KEYS[0]

    stream = MyStreamer(keys['consumer_key'], keys['consumer_secret'],
        keys['access_token_key'], keys['access_token_secret'])
    stream.statuses.filter(locations=[-122.75,36.8,-121.75,37.8])
