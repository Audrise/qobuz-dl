import re
import os
import logging

from mutagen.flac import FLAC, Picture
import mutagen.id3 as id3
from mutagen.id3 import ID3NoHeaderError
from qobuz_dl.settings import QobuzDLSettings
from qobuz_dl.utils import get_album_artist

logger = logging.getLogger(__name__)


# unicode symbols: (C) -> \u00a9 (copyright), (P) -> \u2117 (sound recording copyright)
COPYRIGHT, PHON_COPYRIGHT = "\u00a9", "\u2117"
# if a metadata block exceeds this, mutagen will raise error
# and the file won't be tagged
FLAC_MAX_BLOCKSIZE = 16777215

ID3_LEGEND = {
    "album_artist": id3.TPE2, "album": id3.TALB, "artist": id3.TPE1,
    "title": id3.TIT2, "mediatype": id3.TMED, "genre": id3.TCON,
    "copyright": id3.TCOP, "label": id3.TPUB, "isrc": id3.TSRC,
    "upc": id3.TXXX, "rating": id3.TXXX,
    "QOBUZTRACKID": id3.TXXX, "QOBUZALBUMID": id3.TXXX,
    "QOBUZ ALBUM URL": id3.TXXX,
}

EMB_COVER_NAME = "embed_cover.jpg"
# Primary release types in the MusicBrainz sense, as Navidrome and Picard read RELEASETYPE
PRIMARY_RELEASE_TYPES = ("album", "single", "ep")

LOCAL_GENRE_MAP = {
    # Elettronica & Dance
    "Électronique": "Electronic",
    "Ambiance": "Ambient",
    
    # Classical
    "Classique": "Classical",
    "Musique de chambre": "Chamber Music",
    "Opéra": "Opera",
    "Chorale": "Choral",
    "Symphonique": "Symphonic",
    
    # Soundtracks & Media
    "Bande Originale": "Soundtrack",
    "Musique de film": "Soundtrack",
    "Comédie Musicale": "Musical",
    "Bande originale de jeu vidéo": "Video Game Soundtrack",
    "Séries TV": "TV Series",
    "Bandes originales de films": "Film Soundtracks",
    "Jeux vidéo": "Video Games",
    "Anime/Jeux vidéo": "Anime/Video Game",
    
    # Jazz & Blues
    "Jazz Vocal": "Vocal Jazz",
    "Jazz Contemporain": "Contemporary Jazz",
    
    # World Music
    "Musiques du monde": "World",
    "Musique celtique": "Celtic",
    "Musique latine": "Latin",
    "Variété Française": "French Pop",
    "Alternatif et Indé": "Alternative & Indie",
    "Asie": "Asia",
    "Musique indienne": "Indian Music",
    "Russie": "Russia",
        
    # Others
    "Enfants": "Children's Music",
    "Berceuses": "Lullabies",
    "Poésie et Littérature": "Spoken Word",
    "Livres Audio": "Audiobooks",
    "Humour": "Comedy",
    "Religieux": "Religious",
    "Détente": "Relaxation",
    "Fêtes": "Holiday",
    "Musiques de Noël": "Christmas Music",
    
    # Rock & Pop
    "Rock progressif": "Progressive Rock",
}

def _get_title_with_version(title: str = "", version: str = "") -> str:
    """
    Constructs a track or album title by appending its version if it exists.

    Args:
        title (str, optional): The original title. Defaults to "".
        version (str, optional): The version of the track/album (e.g., 'Remastered'). Defaults to "".

    Returns:
        str: The combined title and version string.
    """
    item_title = title
    if version:
        item_title = (
            f"{title} ({version})"
            if version.lower() not in title.lower()
            else title
        )
    return item_title


def _get_title(track_dict):
    """
    Constructs a comprehensive title for a track, including version and classical work prefixes.

    Args:
        track_dict (dict): The dictionary containing track metadata.

    Returns:
        str: The fully formatted title string.
    """
    title = track_dict["title"]
    version = track_dict.get("version")
    if version:
        title = f"{title} ({version})"
    # for classical works
    if track_dict.get("work"):
        title = f"{track_dict['work']}: {title}"

    return title


def _various_artists_aliases(settings) -> set:
    """Album artist names that mark a compilation, from config.ini (various_artists_aliases)."""
    if settings is not None:
        return settings.various_artists_aliases
    return QobuzDLSettings().various_artists_aliases


def _is_various_artists(qobuz_album: dict, aliases: set) -> bool:
    """True if the album artist is one of the "Various Artists" aliases (a compilation)."""
    names = get_album_artist(qobuz_album) or []
    return any(str(name).strip().casefold() in aliases for name in names)


def _release_types(qobuz_album: dict, aliases: set) -> list:
    """
    Release types for the RELEASETYPE tag, MusicBrainz style: the primary type
    (album, single or ep) followed by "compilation" for compilations.

    Returns:
        list: e.g. ["album"] or ["album", "compilation"]; empty if Qobuz gives no type.
    """
    raw = str(qobuz_album.get("release_type") or qobuz_album.get("product_type") or "").lower()
    types = [raw] if raw in PRIMARY_RELEASE_TYPES else []
    if raw == "compilation" or _is_various_artists(qobuz_album, aliases):
        types = (types or ["album"]) + ["compilation"]
    return types


def _disc_track_total(qobuz_album: dict, qobuz_item: dict) -> str:
    """
    Number of tracks on the item's disc, as TRACKTOTAL is defined per disc.

    Falls back to the album's tracks_count for single-disc albums, single tracks,
    playlists, or when the album's track list is incomplete.
    """
    total = qobuz_album.get("tracks_count") or 1
    items = (qobuz_album.get("tracks") or {}).get("items") or []
    if (qobuz_album.get("media_count") or 1) > 1 and len(items) == total:
        disc = qobuz_item.get("media_number", 1)
        on_disc = sum(1 for track in items if track.get("media_number", 1) == disc)
        if on_disc:
            return str(on_disc)
    return str(total)


def _format_copyright(s: str) -> str:
    """
    Replaces standard text copyright symbols with their Unicode equivalents.

    Args:
        s (str): The original copyright string containing '(P)' or '(C)'.

    Returns:
        str: The formatted copyright string with Unicode symbols.
    """
    if s:
        s = s.replace("(P)", PHON_COPYRIGHT)
        s = s.replace("(C)", COPYRIGHT)
    return s


def _format_genres(genres: list) -> str:
    """
    Cleans and formats a list of genres, removing duplicates and extracting primary names.

    Args:
        genres (list): A list of genre strings from the Qobuz API.

    Returns:
        str: A comma-separated string of unique, formatted genres.
    """
    genres = re.findall(r"([^\u2192\/]+)", "/".join(genres))
    no_repeats = []
    [no_repeats.append(g) for g in genres if g not in no_repeats]
    return ", ".join(no_repeats)


def _embed_flac_img(root_dir, audio: FLAC):
    """
    Embeds a cover image into a FLAC audio file.

    Ensures the image does not exceed the maximum allowed block size for FLAC metadata 
    to prevent encoding errors.

    Args:
        root_dir (str): The directory containing the audio file and cover image.
        audio (FLAC): The Mutagen FLAC audio object to be tagged.
    """
    emb_image = os.path.join(root_dir, EMB_COVER_NAME)
    multi_emb_image = os.path.join(
        os.path.abspath(os.path.join(root_dir, os.pardir)), EMB_COVER_NAME
    )
    if os.path.isfile(emb_image):
        cover_image = emb_image
    else:
        cover_image = multi_emb_image

    if not os.path.isfile(cover_image):
        logger.debug(f"Cover image not found to embed: {cover_image}")
        return

    try:
        if os.path.getsize(cover_image) > FLAC_MAX_BLOCKSIZE:
            raise Exception(
                "downloaded cover size too large to embed. "
                "lower `embedded_art_size` (e.g. 600) to avoid error"
            )

        image = Picture()
        image.type = 3
        image.mime = "image/jpeg"
        image.desc = "cover"
        with open(cover_image, "rb") as img:
            image.data = img.read()
        audio.add_picture(image)
    except Exception as e:
        logger.error(f"Error embedding image: {e}", exc_info=True)


def _embed_id3_img(root_dir, audio: id3.ID3):
    """
    Embeds a cover image into an MP3 file using ID3 tags.

    Args:
        root_dir (str): The directory containing the audio file and cover image.
        audio (id3.ID3): The Mutagen ID3 audio object to be tagged.
    """
    emb_image = os.path.join(root_dir, EMB_COVER_NAME)
    multi_emb_image = os.path.join(
        os.path.abspath(os.path.join(root_dir, os.pardir)), EMB_COVER_NAME
    )
    if os.path.isfile(emb_image):
        cover_image = emb_image
    else:
        cover_image = multi_emb_image

    if not os.path.isfile(cover_image):
        logger.debug(f"Cover image not found to embed: {cover_image}")
        return

    with open(cover_image, "rb") as cover:
        audio.add(id3.APIC(3, "image/jpeg", 3, "", cover.read()))


# Preferred FLAC Vorbis Comment field casing. These are presentation choices;
# Vorbis Comment field names are case-insensitive.
FLAC_TAG_CASE = {
    "TITLE": "Title",
    "ALBUM": "Album",
    "ALBUM_ARTIST": "Album_Artist",
    "ARTIST": "Artist",
    "TRACKNUMBER": "TrackNumber",
    "DISCNUMBER": "DiscNumber",
    "TOTALTRACKS": "TotalTracks",
    "TOTALDISCS": "TotalDiscs",
    "DATE": "Date",
    "COPYRIGHT": "Copyright",
    "RATING": "Rating",
    "GENRE": "Genre",
    "REPLAYGAIN_TRACK_GAIN": "ReplayGain_Track_Gain",
    "REPLAYGAIN_TRACK_PEAK": "ReplayGain_Track_Peak",
    "ISRC": "ISRC",
    "UPC": "UPC",
    "LABEL": "Label",
    "MEDIATYPE": "MediaType",
    "RELEASETYPE": "ReleaseType",
    "QOBUZTRACKID": "QobuzTrackID",
    "QOBUZALBUMID": "QobuzAlbumID",
    "QOBUZ ALBUM URL": "QobuzAlbumURL",
}

def _is_valid_vorbis_field_name(name: str) -> bool:
    """Return whether a Vorbis Comment field name uses valid printable ASCII."""
    if not isinstance(name, str) or not name:
        return False
    return all(0x20 <= ord(char) <= 0x7D and char != "=" for char in name)


def _capitalize_flac_tag_names(tags: dict) -> dict:
    """Apply consistent PascalCase-style names without changing tag values."""
    result = {}
    for key, value in tags.items():
        if key in FLAC_TAG_CASE:
            normalized = FLAC_TAG_CASE[key]
        elif key.upper() in FLAC_TAG_CASE:
            normalized = FLAC_TAG_CASE[key.upper()]
        else:
            # Convert unknown all-uppercase fields segment-by-segment; keep
            # mixed-case dynamic credit roles such as MixingEngineer intact.
            if key == key.upper():
                parts = key.split("_")
                normalized = "_".join(
                    part if part in {"ID", "ISRC", "UPC"} else part[:1].upper() + part[1:].lower()
                    for part in parts
                )
            else:
                normalized = key[:1].upper() + key[1:]
        result[normalized] = value
    return result


def tag_flac(
    filename, root_dir, final_name, d: dict, album, istrack=True, em_image=False, settings: QobuzDLSettings = None, custom_tags: dict = None
):
    """
    Applies metadata tags and cover art to a FLAC audio file.

    Supports advanced tagging features including multi-value tags, ReplayGain injections, 
    and custom Qobuz IDs for local database synchronization. Renames the file to its 
    final destination upon completion.

    Args:
        filename (str): The current path to the audio file.
        root_dir (str): The directory where the file and cover image are located.
        final_name (str): The final designated filename after tagging is complete.
        d (dict): The track or album dictionary containing metadata.
        album (dict): The dictionary containing album-level metadata.
        istrack (bool, optional): Indicates if the file is a single track. Defaults to True.
        em_image (bool, optional): Flag to enable embedding the cover image. Defaults to False.
        settings (QobuzDLSettings, optional): Configuration object for user preferences. Defaults to None.
    """
    settings = settings or QobuzDLSettings()
    audio = FLAC(filename)

    if istrack:
        qobuz_item = d
        qobuz_album = d.get("album", {})
    else:
        qobuz_item = d
        qobuz_album = album

    tags = _get_tags_to_add(qobuz_album, qobuz_item, settings=settings)
    
    if custom_tags:
        tags.update({k: v for k, v in custom_tags.items() if v})

    if not settings.no_track_number_tag:
        tags["TRACKNUMBER"] = str(qobuz_item.get("track_number", "1"))
    if not settings.no_track_total_tag:
        tags["TOTALTRACKS"] = _disc_track_total(qobuz_album, qobuz_item)
    if not settings.no_disc_number_tag:
        tags["DISCNUMBER"] = str(qobuz_item.get("media_number", "1"))
    if not settings.no_disc_total_tag:
        tags["TOTALDISCS"] = str(qobuz_album.get("media_count", "1"))

    tags = _filter_disabled_tags(tags, settings)

    # Normalize all FLAC tag names to the selected Capitalize/PascalCase style.
    tags = _capitalize_flac_tag_names(tags)

    # Keep the core tags in a predictable order when FFmpeg displays the FLAC.
    # The numbering tags are added above for settings compatibility, then moved
    # next to the artist fields instead of being appended at the end.
    preferred_order = (
        "Title", "Album", "Album_Artist", "Artist",
        "TrackNumber", "DiscNumber", "TotalTracks", "TotalDiscs",
        "Date", "Copyright", "Rating", "Genre",
        "ReplayGain_Track_Gain", "ReplayGain_Track_Peak",
        "ISRC", "UPC", "Label",
    )
    ordered_tags = {key: tags[key] for key in preferred_order if key in tags}
    ordered_tags.update({key: value for key, value in tags.items() if key not in ordered_tags})
    tags = ordered_tags

    audio.clear()  # Keep embedded FLAC pictures; replace only Vorbis comments.
    for k, v in tags.items():
        if v:
            # --- MULTI-TAG FEATURE ---
            if getattr(settings, "multi_value_tags", False) and k.casefold() == "genre" and isinstance(v, str):
                values = [value.strip() for value in re.split(r"\s*,\s*", v.strip()) if value.strip()]
                if len(values) > 1:
                    v = values

            # Vorbis Comment field names must be printable ASCII and cannot contain '='.
            # Reject an invalid field instead of letting one unusual credit abort tagging.
            if not _is_valid_vorbis_field_name(k):
                logger.warning("Skipping invalid FLAC metadata field name: %r", k)
                continue

            audio[k] = v

    if em_image:
        _embed_flac_img(root_dir, audio)

    for junk_tag in ["ENCODER", "ENCODED-BY", "ENCODED_BY"]:
        if junk_tag in audio:
            del audio[junk_tag]
            
    if hasattr(audio, 'tags') and audio.tags is not None:
        audio.tags.vendor = ""

    audio.save(padding=lambda info: 8192)
    
    os.rename(filename, final_name)


def tag_mp3(filename, root_dir, final_name, d, album, istrack=True, em_image=False, settings: QobuzDLSettings = None, custom_tags: dict = None):
    """
    Applies ID3 metadata tags and cover art to an MP3 audio file.

    Maps custom Qobuz metadata to corresponding ID3 legends and handles standard track 
    and disc numbering. Renames the file upon completion.

    Args:
        filename (str): The current path to the audio file.
        root_dir (str): The directory where the file and cover image are located.
        final_name (str): The final designated filename after tagging is complete.
        d (dict): The track or album dictionary containing metadata.
        album (dict): The dictionary containing album-level metadata.
        istrack (bool, optional): Indicates if the file is a single track. Defaults to True.
        em_image (bool, optional): Flag to enable embedding the cover image. Defaults to False.
        settings (QobuzDLSettings, optional): Configuration object for user preferences. Defaults to None.
    """
    settings = settings or QobuzDLSettings()
    try:
        audio = id3.ID3(filename)
    except ID3NoHeaderError:
        audio = id3.ID3()

    if istrack:
        qobuz_item = d
        qobuz_album = d.get("album", {})
    else:
        qobuz_item = d
        qobuz_album = album

    tags = _get_tags_to_add(qobuz_album, qobuz_item, settings=settings)
    
    if custom_tags:
        tags.update({k: v for k, v in custom_tags.items() if v})
    tags = _filter_disabled_tags(tags, settings)

    existing_art = audio.getall("APIC") if not em_image else []
    audio.clear()  # Rebuild ID3 tags from the selected metadata fields.
    for picture in existing_art:
        audio.add(picture)

    # ID3v2.3 stores the year in TYER and only the day and month (DDMM) in TDAT
    release_date = tags.pop("DATE", "") or ""
    if release_date[:4].isdigit():
        audio["TYER"] = id3.TYER(encoding=3, text=release_date[:4])
        if release_date[5:7].isdigit() and release_date[8:10].isdigit():
            audio["TDAT"] = id3.TDAT(encoding=3, text=release_date[8:10] + release_date[5:7])

    # Navidrome and Picard read the release type of MP3 files from this frame
    release_types = tags.pop("RELEASETYPE", None)
    if release_types:
        audio.add(id3.TXXX(encoding=3, desc="MusicBrainz Album Type", text=",".join(release_types)))

    for k, v in tags.items():
        if v:
            id3tag = ID3_LEGEND.get(k.lower()) or ID3_LEGEND.get(k)
            if not id3tag and k not in {"RELEASETYPE"}:
                # Qobuz may supply arbitrary credit-role names; store them as TXXX.
                id3tag = id3.TXXX
            if id3tag:
                if id3tag == id3.TXXX:
                    audio.add(id3tag(encoding=3, desc=k, text=v))
                else:
                    audio[id3tag.__name__] = id3tag(encoding=3, text=v)

    # "number/total", following the same tag flags as tag_flac()
    if not settings.no_track_number_tag:
        track = str(qobuz_item.get("track_number", "1"))
        if not settings.no_track_total_tag:
            track += f'/{_disc_track_total(qobuz_album, qobuz_item)}'
        audio["TRCK"] = id3.TRCK(encoding=3, text=track)
    if not settings.no_disc_number_tag:
        disc = str(qobuz_item.get("media_number", "1"))
        if not settings.no_disc_total_tag:
            disc += f'/{str(qobuz_album.get("media_count", "1"))}'
        audio["TPOS"] = id3.TPOS(encoding=3, text=disc)

    if em_image:
        _embed_id3_img(root_dir, audio)

    audio.pop("TENC", None)
    audio.pop("TSSE", None)

    audio.save(filename, v2_version=3)
    os.rename(filename, final_name)


def _parse_performer_credits(performers: str) -> dict:
    """Parse Qobuz's ``Person, Role, Role - Person, Role`` credit string.

    Qobuz may introduce new role names at any time, so roles are not restricted
    to a hard-coded allowlist. In this format, the first comma-separated item
    is the credited person's name and subsequent items are their roles.
    """
    credits = {}
    if not isinstance(performers, str) or not performers.strip():
        return credits

    for block in re.split(r"\s+-\s+", performers.strip()):
        parts = [part.strip() for part in block.split(",") if part.strip()]
        if len(parts) < 2:
            continue

        name = parts[0]
        roles = parts[1:]
        for role in roles:
            # Only accept role labels as tags; don't create empty or malformed keys.
            role = re.sub(r"\s+", " ", role).strip()
            # Dynamic role names become FLAC Vorbis Comment field names too.
            # '=' is forbidden there; reject malformed/control/non-ASCII labels
            # before they can break the entire tagging operation.
            if not role or len(role) > 80 or not _is_valid_vorbis_field_name(role):
                logger.warning("Skipping invalid Qobuz credit role: %r", role)
                continue

            # Merge role labels case-insensitively while retaining the first
            # spelling seen, so Producer and producer do not create two tags.
            existing_role = next(
                (key for key in credits if key.casefold() == role.casefold()),
                role,
            )
            names = credits.setdefault(existing_role, [])
            if name.casefold() not in (existing.casefold() for existing in names):
                names.append(name)
    return credits


def _tag_is_disabled(tag_name: str, settings: QobuzDLSettings) -> bool:
    """Return whether a tag is disabled by the resolved settings."""
    key = re.sub(r"[^a-z0-9]", "", str(tag_name).casefold())
    flag_by_tag = {
        "albumartist": "no_album_artist_tag",
        "album": "no_album_title_tag",
        "artist": "no_track_artist_tag",
        "title": "no_track_title_tag",
        "date": "no_release_date_tag",
        "year": "no_release_date_tag",
        "mediatype": "no_media_type_tag",
        "genre": "no_genre_tag",
        "tracknumber": "no_track_number_tag",
        "tracktotal": "no_track_total_tag",
        "totaltracks": "no_track_total_tag",
        "discnumber": "no_disc_number_tag",
        "disctotal": "no_disc_total_tag",
        "totaldiscs": "no_disc_total_tag",
        "composer": "no_composer_tag",
        "composerlyricist": "no_composer_tag",
        "conductor": "no_conductor_tag",
        "ensemble": "no_ensemble_tag",
        "work": "no_work_tag",
        "rating": "no_explicit_tag",
        "copyright": "no_copyright_tag",
        "label": "no_label_tag",
        "upc": "no_upc_tag",
        "barcode": "no_upc_tag",
        "isrc": "no_isrc_tag",
        "replaygaintrackgain": "no_replaygain_tag",
        "replaygaintrackpeak": "no_replaygain_tag",
        "qobuzalbumurl": "no_album_url_tag",
        "qobuzalbumurltag": "no_album_url_tag",
    }
    flag = flag_by_tag.get(key)
    if flag and getattr(settings, flag, False):
        return True

    # Also cover common spellings used by dynamically parsed Qobuz credit roles.
    if key in {"composerlyricist", "lyricistcomposer"} and getattr(settings, "no_composer_tag", False):
        return True
    return False


def _filter_disabled_tags(tags: dict, settings: QobuzDLSettings) -> dict:
    """Remove tags disabled in config.ini or through CLI-derived settings."""
    return {key: value for key, value in tags.items() if not _tag_is_disabled(key, settings)}


def _get_tags_to_add(qobuz_album: dict, qobuz_item: dict, settings: QobuzDLSettings = None):
    """Build the requested minimal Qobuz metadata profile."""
    settings = settings or QobuzDLSettings()
    tags = {}
    if not qobuz_album or not qobuz_item:
        return tags

    if not settings.no_track_title_tag:
        tags["TITLE"] = _get_title_with_version(qobuz_item.get("title", ""), qobuz_item.get("version", ""))
    if not settings.no_album_title_tag:
        tags["ALBUM"] = _get_title_with_version(qobuz_album.get("title", ""), qobuz_album.get("version", ""))
    if not settings.no_album_artist_tag:
        tags["ALBUM_ARTIST"] = get_album_artist(qobuz_album)
    artist_names = []
    if not settings.no_track_artist_tag:
        main_artist = (qobuz_item.get("performer") or {}).get("name") or (qobuz_album.get("artist") or {}).get("name", "")
        artist_names = [main_artist] if main_artist else []

    if not settings.no_release_date_tag:
        tags["DATE"] = qobuz_album.get("release_date_original", "")
    if not settings.no_copyright_tag:
        tags["COPYRIGHT"] = _format_copyright(qobuz_album.get("copyright", ""))
    if not settings.no_explicit_tag:
        tags["RATING"] = "Explicit" if qobuz_item.get("parental_warning", False) else "Clean"
    if not settings.no_genre_tag:
        genre = (qobuz_album.get("genre") or {}).get("name", "")
        tags["GENRE"] = LOCAL_GENRE_MAP.get(genre, genre)
    if not settings.no_isrc_tag:
        tags["ISRC"] = qobuz_item.get("isrc", "")
    if not settings.no_upc_tag:
        tags["UPC"] = str(qobuz_album.get("upc") or "")
    if not settings.no_label_tag:
        tags["LABEL"] = re.sub(r"\s+", " ", (qobuz_album.get("label") or {}).get("name", "")).strip()

    # Parse every role supplied by Qobuz instead of relying on a fixed role list.
    credits = _parse_performer_credits(qobuz_item.get("performers") or "")
    disabled_credit_roles = set()
    if settings.no_composer_tag:
        disabled_credit_roles.update({"composer", "composerlyricist", "lyricistcomposer"})
    if settings.no_conductor_tag:
        disabled_credit_roles.add("conductor")
    if settings.no_ensemble_tag:
        disabled_credit_roles.add("ensemble")
    if settings.no_work_tag:
        disabled_credit_roles.add("work")

    for role, names in credits.items():
        role_key = re.sub(r"[^a-z0-9]", "", role.casefold())
        # ARTIST already represents the main artist; don't duplicate it as a role tag.
        if role_key == "mainartist" or role_key in disabled_credit_roles:
            continue
        tags[role] = ";".join(names)

    # Prefer all performers explicitly marked as artists, including featured artists.
    if artist_names:
        for role_name in ("MainArtist", "FeaturedArtist", "Artist"):
            for name in credits.get(role_name, []):
                if name.casefold() not in (artist.casefold() for artist in artist_names):
                    artist_names.append(name)
        tags["ARTIST"] = ";".join(artist_names)

    # Track-level ReplayGain is optional; write only values supplied by Qobuz.
    if not getattr(settings, "no_replaygain_tag", False):
        audio_info = qobuz_item.get("audio_info") or {}
        rg_gain = audio_info.get("replaygain_track_gain")
        rg_peak = audio_info.get("replaygain_track_peak")
        if rg_gain is not None:
            gain = str(rg_gain)
            tags["REPLAYGAIN_TRACK_GAIN"] = gain if gain.lower().endswith("db") else f"{gain} dB"
        if rg_peak is not None:
            tags["REPLAYGAIN_TRACK_PEAK"] = str(rg_peak)

    if not settings.no_media_type_tag:
        tags["MEDIATYPE"] = (qobuz_album.get("product_type") or "").upper()
    release_types = _release_types(qobuz_album, _various_artists_aliases(settings))
    if release_types:
        tags["RELEASETYPE"] = release_types

    track_id = qobuz_item.get("id")
    album_id = qobuz_album.get("id")
    if track_id:
        tags["QOBUZTRACKID"] = str(track_id)
    if album_id:
        tags["QOBUZALBUMID"] = str(album_id)
        if not getattr(settings, "no_album_url_tag", False):
            slug = re.sub(r"[^a-z0-9]+", "-", str(qobuz_album.get("title", "album")).lower()).strip("-")
            tags["QOBUZ ALBUM URL"] = f"https://www.qobuz.com/album/{slug}/{album_id}"
    return tags
