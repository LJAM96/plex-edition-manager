import os
import re
import sys
import time
import json
import logging
import requests
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from configparser import ConfigParser
from threading import Lock

_progress_lock = Lock()
_progress_total = 1
_progress_done = 0
_progress_last_emit = -1.0

CACHE_FILE = Path(__file__).parent / 'config' / 'progress-cache.json'
_cache_lock = Lock()
_progress_cache = {}
_progress_cache_dirty = 0

def _emit_progress(pct: float):
    print(f"PROGRESS {pct:.4f} {_progress_done} {_progress_total}")
    sys.stdout.flush()

def _progress_set_total(n: int):
    global _progress_total, _progress_done, _progress_last_emit
    with _progress_lock:
        _progress_total = max(1, int(n))
        _progress_done = 0
        _progress_last_emit = -1.0
        _emit_progress(0.0)

def _progress_step(k: int = 1):
    global _progress_done, _progress_total, _progress_last_emit
    with _progress_lock:
        _progress_done = min(_progress_total, _progress_done + k)
        pct = (_progress_done * 100) / _progress_total
        min_delta = max(0.1, 100 / max(1, _progress_total))
        should_emit = _progress_last_emit < 0 or pct >= 100 or pct - _progress_last_emit >= min_delta
        if should_emit:
            _progress_last_emit = pct
            _emit_progress(min(100.0, max(0.0, pct)))


def load_progress_cache():
    global _progress_cache
    with _cache_lock:
        if CACHE_FILE.exists():
            try:
                _progress_cache = json.loads(CACHE_FILE.read_text(encoding='utf-8'))
            except Exception:
                _progress_cache = {}
        else:
            _progress_cache = {}


def save_progress_cache(force: bool = False):
    global _progress_cache_dirty
    with _cache_lock:
        if not force and _progress_cache_dirty < 25:
            return
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_FILE.open('w', encoding='utf-8') as fh:
            json.dump(_progress_cache, fh, indent=2)
        _progress_cache_dirty = 0


def _movie_signature(movie: dict) -> str:
    updated = movie.get('updatedAt') or movie.get('addedAt') or 0
    duration = movie.get('duration') or 0
    media = movie.get('Media') or []
    size = 0
    if media:
        parts = media[0].get('Part') or []
        if parts:
            size = parts[0].get('size', 0)
    return f"{updated}-{duration}-{size}"


def should_skip_movie(movie: dict) -> bool:
    key = str(movie.get('ratingKey') or '')
    if not key:
        return False
    signature = _movie_signature(movie)
    with _cache_lock:
        entry = _progress_cache.get(key)
        return bool(entry and entry.get('signature') == signature)


def mark_movie_processed(movie: dict):
    global _progress_cache_dirty
    key = str(movie.get('ratingKey') or '')
    if not key:
        return
    signature = _movie_signature(movie)
    if not signature:
        return
    with _cache_lock:
        existing = _progress_cache.get(key)
        if existing and existing.get('signature') == signature:
            return
        _progress_cache[key] = {
            'signature': signature,
            'title': movie.get('title', ''),
        }
        _progress_cache_dirty += 1
    save_progress_cache(force=False)

# Create a logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Thread-local storage for requests session
thread_local = threading.local()

def get_session():
    """Get thread-local session for connection pooling"""
    if not hasattr(thread_local, "session"):
        thread_local.session = requests.Session()
    return thread_local.session

HTTP_TIMEOUT = 30

def make_request(url, headers, timeout=HTTP_TIMEOUT):
    session = get_session()
    response = session.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()

# Initialize settings
def initialize_settings():
    config_file = Path(__file__).parent / 'config' / 'config.ini'
    config = ConfigParser()
    config.read(config_file)
    if 'server' in config.sections():
        server = config.get('server', 'address')
        token = config.get('server', 'token')
        skip_libraries = set(re.split(r'[；;]', config.get('server', 'skip_libraries'))) if config.has_option('server', 'skip_libraries') else set()
        modules = re.split(r'[；;]', config.get('modules', 'order')) if config.has_option('modules', 'order') else ['Resolution', 'Duration', 'Rating', 'Cut', 'Release', 'DynamicRange', 'Country', 'ContentRating', 'Language', 'AudioChannels', 'Director', 'Genre', 'SpecialFeatures', 'Studio', 'AudioCodec', 'Bitrate', 'FrameRate', 'Size', 'Source', 'VideoCodec']
        
        excluded_languages = set()
        if config.has_option('language', 'excluded_languages'):
            excluded_languages = set(lang.strip() for lang in re.split(r'[,;]', config.get('language', 'excluded_languages')))
        
        # Performance settings
        max_workers = config.getint('performance', 'max_workers', fallback=10)
        batch_size = config.getint('performance', 'batch_size', fallback=25)
        http_timeout = config.getint('performance', 'http_timeout', fallback=HTTP_TIMEOUT)
        globals()['HTTP_TIMEOUT'] = max(5, http_timeout)
        
        try:
            # Test connection
            headers = {'X-Plex-Token': token, 'Accept': 'application/json'}
            response = make_request(f'{server}/library/sections', headers)
            server_name = response['MediaContainer'].get('friendlyName', server)
            logger.info(f"Successfully connected to server: {server_name}")
        except requests.exceptions.RequestException as err:
            logger.error("Server connection failed, please check the settings in the configuration file or your network. For help, please visit https://github.com/x1ao4/edition-manager-for-plex for instructions.")
            time.sleep(10)
            raise SystemExit(err)
        return server, token, skip_libraries, modules, excluded_languages, max_workers, batch_size

# Batched movie processing
def process_movies_batch(movies_batch, server, token, modules, excluded_languages, lib_title=""):
    """Process a batch of movies in parallel"""
    for movie in movies_batch:
        try:
            process_single_movie(server, token, movie, modules, excluded_languages)
        except Exception as e:
            logger.error(f"Error processing movie {movie.get('title', 'Unknown')}: {str(e)}")

# Main movie processing function (now with threading)
def process_movies(server, token, skip_libraries, modules, excluded_languages, max_workers, batch_size):
    headers = {'X-Plex-Token': token, 'Accept': 'application/json'}
    libraries = make_request(f'{server}/library/sections', headers)['MediaContainer']['Directory']

    # Gather all movies across selected movie libraries
    all_movies = []
    library_info = {}
    for library in libraries:
        if library.get('type') == 'movie' and library.get('title') not in skip_libraries:
            lib_title = library.get('title')
            resp = make_request(f"{server}/library/sections/{library['key']}/all", headers)
            movies = resp.get('MediaContainer', {}).get('Metadata', []) if resp else []
            all_movies.extend(movies)
            library_info[lib_title] = len(movies)

    logger.info(f"Total movies found: {len(all_movies)}")
    for lib_title, count in library_info.items():
        logger.info(f"Library: {lib_title}, Movies: {count}")

    pending_movies = []
    skipped = 0
    for movie in all_movies:
        if should_skip_movie(movie):
            skipped += 1
            continue
        pending_movies.append(movie)

    if skipped:
        logger.info(f"Skipping {skipped} movies that match the cache. {len(pending_movies)} remain.")
    if not pending_movies:
        logger.info("All movies are up to date according to the cache. Remove progress-cache.json to force a full run.")
        return

    _progress_set_total(len(pending_movies))

    # Submit batches, but wait for each batch to finish before submitting the next (backpressure)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for i in range(0, len(pending_movies), batch_size):
            batch = pending_movies[i:i+batch_size]
            futures = [executor.submit(process_single_movie, server, token, m, modules, excluded_languages) for m in batch]
            for _ in as_completed(futures):
                _progress_step()
            logger.info(f"Processed batch {i//batch_size + 1}/{(len(pending_movies)+batch_size-1)//batch_size}")

    save_progress_cache(force=True)

# Process a single movie (without caching)
def process_single_movie(server, token, movie, modules, excluded_languages):
    headers = {'X-Plex-Token': token, 'Accept': 'application/json'}
    movie_id = movie['ratingKey']
    
    # Fetch detailed movie data if needed
    detailed_movie = None
    try:
        detailed_response = get_session().get(
            f'{server}/library/metadata/{movie_id}', headers=headers, timeout=HTTP_TIMEOUT
        )
        if detailed_response.status_code == 200:
            detailed_data = detailed_response.json()
            if 'MediaContainer' in detailed_data and 'Metadata' in detailed_data['MediaContainer']:
                detailed_movie = detailed_data['MediaContainer']['Metadata'][0]
    except Exception as e:
        logger.warning(f"Could not fetch detailed metadata for movie {movie.get('title', 'Unknown')}: {str(e)}")
    
    # Use detailed_movie when available, otherwise fall back to basic movie data
    movie_data = detailed_movie if detailed_movie else movie
    
    media_parts = movie_data['Media'][0]['Part']
    if media_parts:
        max_size_part = max(media_parts, key=lambda part: part['size'])
        file_path = max_size_part['file']
        file_name = os.path.basename(file_path)
        tags = []
        
        from modules.Resolution import get_Resolution
        from modules.Duration import get_Duration
        from modules.Rating import get_Rating
        from modules.Cut import get_Cut
        from modules.Release import get_Release
        from modules.DynamicRange import get_DynamicRange
        from modules.Country import get_Country
        from modules.ContentRating import get_ContentRating
        from modules.Language import get_Language
        from modules.AudioChannels import get_AudioChannels
        from modules.Director import get_Director
        from modules.Genre import get_Genre
        from modules.SpecialFeatures import get_SpecialFeatures
        from modules.Studio import get_Studio
        from modules.AudioCodec import get_AudioCodec
        from modules.Bitrate import get_Bitrate
        from modules.FrameRate import get_FrameRate
        from modules.Size import get_Size
        from modules.Source import get_Source
        from modules.VideoCodec import get_VideoCodec
        
        # Process each module
        for module in modules:
            try:
                if module == 'Resolution':
                    Resolution = get_Resolution(movie_data)
                    if Resolution:
                        tags.append(Resolution)
                elif module == 'Duration':
                    Duration = get_Duration(movie_data)
                    if Duration:
                        tags.append(Duration)
                elif module == 'Rating':
                    Rating = get_Rating(movie_data, server, token, movie_id)
                    if Rating:
                        tags.append(Rating)
                elif module == 'Cut':
                    Cut = get_Cut(file_name, server, token, movie_id)
                    if Cut:
                        tags.append(Cut)
                elif module == 'Release':
                    Release = get_Release(file_name, server, token, movie_id)
                    if Release:
                        tags.append(Release)
                elif module == 'DynamicRange':
                    DynamicRange = get_DynamicRange(server, token, movie_id)
                    if DynamicRange:
                        tags.append(DynamicRange)
                elif module == 'Country':
                    Country = get_Country(server, token, movie_id)
                    if Country:
                        tags.append(Country)
                elif module == 'ContentRating':
                    ContentRating = get_ContentRating(movie_data)
                    if ContentRating:
                        tags.append(ContentRating)
                elif module == 'Language':
                    Language = get_Language(server, token, movie_id, excluded_languages)
                    if Language:
                        tags.append(Language)
                elif module == 'AudioChannels':
                    AudioChannels = get_AudioChannels(movie_data)
                    if AudioChannels:
                        tags.append(AudioChannels)
                elif module == 'Director':
                    Director = get_Director(movie_data)
                    if Director:
                        tags.append(Director)
                elif module == 'Genre':
                    Genre = get_Genre(movie_data)
                    if Genre:
                        tags.append(Genre)
                elif module == 'SpecialFeatures':
                    SpecialFeatures = get_SpecialFeatures(movie_data)
                    if SpecialFeatures:
                        tags.append(SpecialFeatures)
                elif module == 'Studio':
                    Studio = get_Studio(movie_data)
                    if Studio:
                        tags.append(Studio)
                elif module == 'AudioCodec':
                    AudioCodec = get_AudioCodec(server, token, movie_id)
                    if AudioCodec:
                        tags.append(AudioCodec)
                elif module == 'Bitrate':
                    Bitrate = get_Bitrate(server, token, movie_id)
                    if Bitrate:
                        tags.append(Bitrate)
                elif module == 'FrameRate':
                    FrameRate = get_FrameRate(movie_data)
                    if FrameRate:
                        tags.append(FrameRate)
                elif module == 'Size':
                    Size = get_Size(server, token, movie_id)
                    if Size:
                        tags.append(Size)
                elif module == 'Source':
                    Source = get_Source(file_name, server, token, movie_id)
                    if Source:
                        tags.append(Source)
                elif module == 'VideoCodec':
                    VideoCodec = get_VideoCodec(movie_data)
                    if VideoCodec:
                        tags.append(VideoCodec)
            except Exception as e:
                logger.error(f"Error processing module {module} for {movie_data.get('title', 'Unknown')}: {str(e)}")
        
        # Always call update_movie, even if tags is empty
        update_movie(server, token, movie_data, tags, modules)
        mark_movie_processed(movie_data)

def update_movie(server, token, movie, tags, modules):
    movie_id = movie['ratingKey']
    title = movie.get('title', 'Unknown')
    
    # Clear existing edition title and rating
    clear_params = {
        'type': 1,
        'id': movie_id,
        'editionTitle.value': '',
        'editionTitle.locked': 0,
        'rating.value': '',
        'rating.locked': 0
    }
    
    session = get_session()
    session.put(f'{server}/library/metadata/{movie_id}', headers={'X-Plex-Token': token}, params=clear_params)
    
    # Remove duplicates while preserving order
    tags = list(dict.fromkeys(tags))

    if tags:
        edition_title = ' · '.join(tags)
        params = {
            'type': 1,
            'id': movie_id,
            'editionTitle.value': edition_title,
            'editionTitle.locked': 1
        }
        
        # If 'Rating' is in modules, add it to params
        if 'Rating' in modules and any(tag.replace('.', '').isdigit() for tag in tags):
            rating = next(tag for tag in tags if tag.replace('.', '').isdigit())
            params['rating.value'] = rating
            params['rating.locked'] = 1
        
        session.put(f'{server}/library/metadata/{movie_id}', headers={'X-Plex-Token': token}, params=params)
        logger.info(f'{title}: {edition_title}')
    else:
        logger.info(f'{title}: Cleared edition information')
    
    return True

# Reset movies with multi-threading
def reset_movies(server, token, skip_libraries, max_workers, batch_size):
    headers = {'X-Plex-Token': token, 'Accept': 'application/json'}
    libraries = make_request(f'{server}/library/sections', headers)['MediaContainer']['Directory']

    to_reset = []
    for lib in libraries:
        if lib.get('type') == 'movie' and lib.get('title') not in skip_libraries:
            resp = make_request(f"{server}/library/sections/{lib['key']}/all", headers)
            movies = resp.get('MediaContainer', {}).get('Metadata', []) if resp else []
            to_reset.extend([m for m in movies if 'editionTitle' in m])

    logger.info(f"Total movies to reset: {len(to_reset)}")
    _progress_set_total(len(to_reset))

    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _reset_one(movie):
        movie_id = movie['ratingKey']
        params = {'type': 1, 'id': movie_id, 'editionTitle.value': '', 'editionTitle.locked': 0}
        s = get_session()
        s.put(f'{server}/library/metadata/{movie_id}', headers={'X-Plex-Token': token}, params=params)
        logger.info(f"Reset: {movie.get('title', 'Unknown')}")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for i in range(0, len(to_reset), batch_size):
            batch = to_reset[i:i+batch_size]
            futures = [ex.submit(_reset_one, m) for m in batch]
            for _ in as_completed(futures):
                _progress_step()
            logger.info(f"Reset batch {i//batch_size + 1}/{(len(to_reset)+batch_size-1)//batch_size}")

# Reset a single movie
def reset_movie(server, token, movie):
    movie_id = movie['ratingKey']
    title = movie.get('title', 'Unknown')
    
    try:
        params = {'type': 1, 'id': movie_id, 'editionTitle.value': '', 'editionTitle.locked': 0}
        session = get_session()
        session.put(f'{server}/library/metadata/{movie_id}', headers={'X-Plex-Token': token}, params=params)
        logger.info(f'Reset {title}')
        return True
    except Exception as e:
        logger.error(f"Error resetting movie {title}: {str(e)}")
        return False

# Backup metadata
def backup_metadata(server, token, backup_file):
    headers = {'X-Plex-Token': token, 'Accept': 'application/json'}
    libraries = make_request(f'{server}/library/sections', headers)['MediaContainer']['Directory']
    metadata = {}
    for lib in libraries:
        if lib.get('type') == 'movie':
            response = make_request(f"{server}/library/sections/{lib['key']}/all", headers)
            for movie in response['MediaContainer'].get('Metadata', []):
                metadata[movie['ratingKey']] = {
                    'title': movie.get('title', ''),
                    'editionTitle': movie.get('editionTitle', '')
                }

    backup_file = Path(backup_file)  # normalize in case a string is passed
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    with backup_file.open('w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2)
    print(f"Backup complete. {len(metadata)} movies saved to {backup_file}")

# Improved restore metadata function
def restore_metadata(server, token, backup_file):
    with open(backup_file, 'r', encoding='utf-8') as f:
        metadata = json.load(f)

    items = list(metadata.items())
    logger.info(f"Starting restore for {len(items)} movies")
    _progress_set_total(len(items))

    def _restore_one(pair):
        movie_id, meta = pair
        edition = meta.get('editionTitle', '')
        params = {'type': 1, 'id': movie_id, 'editionTitle.value': edition, 'editionTitle.locked': 1 if edition else 0}
        s = get_session()
        s.put(f'{server}/library/metadata/{movie_id}', headers={'X-Plex-Token': token}, params=params)
        _progress_step()

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_restore_one, p) for p in items]
        for _ in as_completed(futures):
            pass

    logger.info("Restore complete.")

# Main function
def main():
    server, token, skip_libraries, modules, excluded_languages, max_workers, batch_size = initialize_settings()
    load_progress_cache()
    
    parser = argparse.ArgumentParser(description='Manage Plex server movie editions')
    parser.add_argument('--all', action='store_true', help='Add edition info to all movies')
    parser.add_argument('--reset', action='store_true', help='Reset edition info for all movies')
    parser.add_argument('--backup', action='store_true', help='Backup movie metadata')
    parser.add_argument('--restore', action='store_true', help='Restore movie metadata from backup')
    
    args = parser.parse_args()
    
    backup_file = Path(__file__).parent / 'metadata_backup' / 'metadata_backup.json'
    
    if args.backup:
        backup_metadata(server, token, backup_file)
        logger.info('Metadata backup completed.')
    elif args.restore:
        restore_metadata(server, token, backup_file)
        logger.info('Metadata restoration completed.')
    elif args.all:
        process_movies(server, token, skip_libraries, modules, excluded_languages, max_workers, batch_size)
    elif args.reset:
        reset_movies(server, token, skip_libraries, max_workers, batch_size)
    else:
        logger.info('No action specified. Please use one of the following arguments:')
        logger.info('  --all: Add edition info to all movies')
        logger.info('  --reset: Reset edition info for all movies')
        logger.info('  --backup: Backup movie metadata')
        logger.info('  --restore: Restore movie metadata from backup')
    
    logger.info('Script execution completed.')

if __name__ == '__main__':
    main()
