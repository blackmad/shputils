#!/usr/bin/python

import logging
import sys

from shapely.geometry import mapping, shape
from shapely.geometry import Point

from fiona import collection

import itertools

import psycopg2
import psycopg2.extras

import re
import unicodedata


# these should all be command-line opts, I am feeling lazy
conn = psycopg2.connect("dbname='geonames' user='blackmad' host='localhost' password='xxx'")
shp_name_cols = ['qs_name', 'qs_name_al']
shp_cc_col = 'qs_iso_cc'
allowed_gn_classes = ['P']
allowed_gn_codes = []

fallback_allowed_gn_classes = []
fallback_allowed_gn_codes = ['ADM4']
 
inputFile = sys.argv[1] 
outputFile = sys.argv[2] 
# set to 0 or None to take all
maxFeaturesToProcess = 0

def take(n, iterable):
  return itertools.islice(iterable, n)

def levenshtein(a,b):
    "Calculates the Levenshtein distance between a and b."
    n, m = len(a), len(b)
    if n > m:
        # Make sure n <= m, to use O(min(n,m)) space
        a,b = b,a
        n,m = m,n
        
    current = range(n+1)
    for i in range(1,m+1):
        previous, current = current, [i]+[0]*n
        for j in range(1,n+1):
            add, delete = previous[j]+1, current[j-1]+1
            change = previous[j-1]
            if a[j-1] != b[i-1]:
                change = change + 1
            current[j] = min(add, delete, change)
    return current[n]


format = '%(name)s - %(levelname)s - %(message)s'
logging.basicConfig(level=logging.INFO, filename='debug.log', format=format)

logger = logging.getLogger('gn_matcher')

failureh = logging.FileHandler('failure.log')
failureh.setLevel(logging.DEBUG)
ambiguoush = logging.FileHandler('ambiguous.log')
ambiguoush.setLevel(logging.DEBUG)

matchLogger = logging.getLogger('match')
ambiguousLogger = logging.getLogger('ambiguous')
ambiguousLogger.addHandler(ambiguoush)
failureLogger = logging.getLogger('failure')
failureLogger.addHandler(failureh)

def hack_feature_name(n):
  n = n.replace('(Rhld.)', '')
  return n

def remove_diacritics(char):
    '''
    Return the base character of char, by "removing" any
    diacritics like accents or curls and strokes and the like.
    '''
    desc = unicodedata.name(unicode(char))
    cutoff = desc.find(' WITH ')
    if cutoff != -1:
        desc = desc[:cutoff]
    return unicodedata.lookup(desc)

def remove_accents(input_str):
    nkfd_form = unicodedata.normalize('NFKD', input_str)
    return u"".join([c for c in nkfd_form if not unicodedata.combining(c)])

def get_feature_names(f):
  feature_names = filter(None, [f['properties'][col] for col in shp_name_cols])
  if f['properties'][shp_cc_col] == 'DE':
    feature_names = [re.sub(' \(.*\)', '', n) for n in feature_names]
  feature_names = [remove_accents(n).lower() for n in feature_names]
  return feature_names

def get_geoname_names_for_matching(gn_candidate):
  feature_names = filter(None, [gn_candidate['name'], gn_candidate['asciiname']] + (gn_candidate['alternatenames'] or '').split(','))
  feature_names = [n.decode('utf-8') for n in feature_names]
  if gn_candidate['cc2'] == 'DE':
    feature_names = [re.sub(' \(.*\)', '', n) for n in feature_names]
  feature_names = [remove_accents(n).lower() for n in feature_names]
  return feature_names

def does_feature_match(f, gn_candidate):
  point = Point(gn_candidate['latitude'], gn_candidate['longitude'])
  candidate_names = get_geoname_names_for_matching(gn_candidate)
  feature_names = get_feature_names(f)

  # for each input name in the shape, see if we have a match in a geoname feature
  for f_name in feature_names:
    for gn_name in candidate_names:
      distance = levenshtein(f_name, gn_name)
      logger.debug(u'%s vs %s -- distance %d' % (f_name, gn_name, distance))
      if distance <= 1 or ((distance*1.0) / len(f_name) < 0.14):
        matchLogger.debug(u'%s vs %s -- distance %d -- GOOD ENOUGH' % (f_name, gn_name, distance))
        return True
      else:
        matchLogger.debug(u'%s vs %s -- distance %d -- NO' % (f_name, geoname_debug_str(gn_candidate), distance))

  return False

def get_feature_name(f):
  feature_names = filter(None, [f['properties'][col] for col in shp_name_cols])
  return feature_names[0]

def get_geoname_name(gn):
  return gn['name'].decode('utf-8')

def get_geoname_id(gn):
  return str(gn['geonameid'])

def get_geoname_fclass(gn):
  return (gn['fclass'] or '').decode('utf-8')

def get_geoname_fcode(gn):
  return (gn['fcode'] or '').decode('utf-8')

def geoname_debug_str(gn):
  return u"%s %s %s %s" % (get_geoname_name(gn), get_geoname_id(gn), get_geoname_fclass(gn), get_geoname_fcode(gn))

def get_feature_debug(f):
  return u"%s (%s)" % (get_feature_name(f), shape(f['geometry']).centroid)

def main():
  input = collection(inputFile, "r")

  newSchema = input.schema.copy()
  newSchema['geonameid'] = 'str:1000'
  output = collection(
    outputFile, 'w', 'ESRI Shapefile', newSchema, crs=input.crs, encoding='utf-8')

  inputIter = input
  if maxFeaturesToProcess:
    inputIter = take(maxFeaturesToProcess, input)
  num_elems = len(inputIter)
  num_matched = 0
  num_failed = 0
  num_skipped = 0
  num_ambiguous = 0
  num_fallback = 0
  num_zero_candidates = 0

  for i,f in enumerate(inputIter):
    if i % 1000 == 0:
      sys.stderr.write('finished %d of %d (success %s (fallback: %s), ambiguous: %s, skipped %s, failed %s (zero-candidates: %s))\n' % (i, num_elems, num_matched, num_fallback, num_ambiguous, num_skipped, num_failed, num_zero_candidates))
    # Make a shapely object from the dict.
    geom = shape(f['geometry'])
    if not geom.is_valid:
      # Use the 0-buffer polygon cleaning trick
      clean = geom.buffer(0.0)
      geom = clean
    if f["geometry"]["type"] not in ('Polygon', 'MultiPolygon'):
      print 'skipping %s due to it not being a polygon -- you could fix this with a radius if you wanted' % get_feature_debug(f)
      num_skipped += 1
    elif geom.is_valid:
      cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
      cur.execute("""select * FROM geoname WHERE the_geom && ST_SetSRID(ST_MakeBox2D(ST_MakePoint(%s, %s), ST_MakePoint(%s, %s)), 4326) AND (fclass IN %s OR fcode IN %s)""",
        (
          geom.bounds[0],
          geom.bounds[1],
          geom.bounds[2],
          geom.bounds[3],
          tuple(allowed_gn_classes + fallback_allowed_gn_classes),
          tuple(allowed_gn_codes + fallback_allowed_gn_codes)
        )
      )
      matches = []
      failures = []
      rows = cur.fetchall()

      matchLogger.error(u'found 0 candidates for %s' % (get_feature_debug(f)))
      failureLogger.error(u'found 0 candidates for %s' % (get_feature_debug(f)))
      num_zero_candidates += 1
      for gn_candidate in rows:
        if does_feature_match(f, gn_candidate):
          matches.append(gn_candidate)
        else:
          failures.append(gn_candidate)

      final_matches = filter(lambda gn_candidate: (get_geoname_fclass(gn_candidate) in allowed_gn_classes) or (get_geoname_fcode(gn_candidate) in allowed_gn_codes), matches)
      fallback_matches = filter(lambda gn_candidate: (get_geoname_fclass(gn_candidate) in fallback_allowed_gn_classes) or (get_geoname_fcode(gn_candidate) in fallback_allowed_gn_codes), matches)
      if len(final_matches) == 0 and len(fallback_matches) > 0:
        matchLogger.debug('0 preferred type matches, falling back to fallback match types')
        final_matches = fallback_matches
        num_fallback += 1

      if len(final_matches) == 0:
        f['geonameid'] = None
        failureLogger.error(u'found 0 match for %s' % (get_feature_debug(f)))
        for m in rows:
          failureLogger.error('\t' + geoname_debug_str(m))
        num_failed += 1
      elif len(final_matches) == 1:
        m = final_matches[0]
        matchLogger.debug(u'found 1 match for %s:' % (get_feature_debug(f)))
        matchLogger.debug('\t' + geoname_debug_str(m))
        f['geonameid'] = ','.join([get_geoname_id(m) for m in final_matches])
        num_matched += 1
      elif len(final_matches) > 1:
        ambiguousLogger.error(u'found multiple final_matches for %s:' % (get_feature_debug(f)))
        for m in final_matches:
          ambiguousLogger.error('\t' + geoname_debug_str(m))
        f['geonameid'] = ','.join([get_geoname_id(m) for m in final_matches])
        num_ambiguous += 1

main()