import datetime
import logging
import os
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .models import Base, Episode, Song, XMState
from .utils import get_files, splice_file

logger = logging.getLogger('discord_siriusxm.processor')


def init_db(base_folder, reset=False):
    os.makedirs(base_folder, exist_ok=True)

    song_db = os.path.join(base_folder, 'songs.db')

    if reset and os.path.exists(song_db):
        os.remove(song_db)

    db_engine = create_engine(f'sqlite:///{song_db}')
    Base.metadata.create_all(db_engine)
    db_session = sessionmaker(bind=db_engine)()

    return db_session


def path_filter(word):
    return word\
        .replace('Counterfeit.', 'Counterfeit')\
        .replace('F**ker', 'Fucker')\
        .replace('Trust?', 'Trust')\
        .strip()


def process_cut(archives, db, cut, output_folder,
                active_channel_id, is_song=True):
    archive = None
    start = int(cut.time / 1000) + 20
    padded_duration = int(cut.duration + 20)
    end = start + padded_duration

    for archive_key, archive_file in archives.items():
        archive_start, archive_end = archive_key.split('.')
        archive_start, archive_end = int(archive_start), int(archive_end)

        if archive_start < start and archive_end > end:
            archive = archive_file
            start = start - archive_start
            end = start + padded_duration
            break

    if archive is not None:
        logger.debug(f'found archive {archive}')

        title = None
        album_or_show = None
        artist = None
        filename = None
        folder = None

        if is_song:
            title = path_filter(cut.cut.title)
            artist = path_filter(cut.cut.artists[0].name)

            if cut.cut.album is not None and cut.cut.album.title is not None:
                album_or_show = path_filter(cut.cut.album.title)

            filename = f'{title}.{cut.guid}.mp3'
            folder = os.path.join(output_folder, artist)

            if album_or_show is not None:
                folder = os.path.join(folder, album_or_show)
        else:
            title = path_filter(cut.episode.long_title or
                                cut.episode.medium_title)

            if cut.episode.show is not None:
                album_or_show = path_filter(cut.episode.show.long_title or
                                            cut.episode.show.medium_title)

            air_time = datetime.fromtimestamp(
                cut.time, tz=datetime.timezone.utc)
            air_time = air_time.replace(minute=0, second=0, microsecond=0)
            filename = f'{title}.{air_time.isoformat()}.{cut.guid}.mp3'
            folder = output_folder

            if album_or_show is not None:
                folder = os.path.join(folder, album_or_show)

        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, filename)
        logger.debug(f'{cut.duration}: {path}')
        path = splice_file(archive, path, start, end)

        # try:
        #     song_path = trim_song(song_path, cut.duration)
        # except Exception as e:
        #     logger.error(f'error occurred in trimming: {e}')

        if path is not None:
            db_item = None

            if is_song:
                db_item = Song(
                    guid=cut.guid,
                    title=title,
                    artist=artist,
                    album=album_or_show,
                    channel=active_channel_id,
                    file_path=path
                )
            else:
                db_item = Episode(
                    guid=cut.guid,
                    title=title,
                    show=album_or_show,
                    air_time=air_time,
                    channel=active_channel_id,
                    file_path=path
                )

            db.add(db_item)
            db.commit()
            logger.debug(f'inserted cut {is_song}: {db_item.guid}')
            return True
    return False


def process_cuts(archives, db, output_folder, channel_id, cuts, is_song=True):
    logger.warn(f'processing: {len(cuts)}: {is_song}')

    processed = 0
    for cut in cuts:
        if cut.duration == 0.0:
            continue

        db_item = None
        if is_song:
            db_item = db.query(Song).filter_by(guid=cut.guid).first()
        else:
            db_item = db.query(Episode).filter_by(guid=cut.guid).first()

        if db_item is not None:
            continue

        title = None
        if is_song:
            title = cut.cut.title
        else:
            title = cut.episode.long_title or \
                cut.episode.medium_title

        logger.warn(
            f'processing {title}: '
            f'{cut.time}: {cut.duration}'
        )
        success = process_cut(
            archives, db, cut, output_folder, channel_id, is_song)

        if success:
            processed += 1
    return processed


def run_processor(state, output_folder, reset_songs):

    state = XMState(state)

    processed_folder = os.path.join(output_folder, 'processed')
    archive_folder = os.path.join(output_folder, 'archive')

    os.makedirs(processed_folder, exist_ok=True)
    os.makedirs(archive_folder, exist_ok=True)

    db = init_db(processed_folder, reset_songs)

    logger.warn(f'processor started: {output_folder}')
    sleep_time = 10
    while True:
        time.sleep(sleep_time)
        sleep_time = 600

        try:
            active_channel_id = state.active_channel_id

            if active_channel_id is None or \
                    state.live is None:
                continue

            channel_archive = os.path.join(archive_folder, active_channel_id)
            channel_folder = os.path.join(processed_folder, active_channel_id)

            song_folder = os.path.join(channel_folder, 'songs')
            shows_folder = os.path.join(channel_folder, 'shows')

            os.makedirs(song_folder, exist_ok=True)

            archives = {}
            archive_files = get_files(channel_archive)
            for archive_file in archive_files:
                file_parts = archive_file.split('.')
                archive_key = f'{file_parts[1]}.{file_parts[2]}'
                archives[archive_key] = os.path.join(
                    channel_archive, archive_file)
            logger.debug(f'found {len(archives.keys())}')

            processed_songs = process_cuts(
                archives, db, song_folder,
                active_channel_id, state.live.song_cuts,
                is_song=True
            )

            processed_shows = process_cuts(
                archives, db, shows_folder,
                active_channel_id, state.live.episode_markers,
                is_song=False
            )

            logger.warn(
                f'processed: {processed_songs} songs, {processed_shows} shows')
        except Exception as e:
            logger.error(f'error occurred in processor loop: {e}')
