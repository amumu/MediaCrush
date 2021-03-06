from mediacrush.objects import File, RedisObject
from mediacrush.database import r, _k
from mediacrush.fileutils import file_storage

from mediacrush.processing.invocation import Invocation
from mediacrush.config import _cfg, _cfgi
import sys
import json

# This does file type detection by inspection rather than extensions
# It will return 'video' or 'audio' for anything ffmpeg supports
# It will return the full mimetype for PNG, JPG, BMP, and SVG files, which we have
# special procedures to optimize
# It will return 'image' for all other images that imagemagick supports
# It will return None for things we can't handle
# Will also try to detect plaintext files (including various kinds of source code)
# These will return 'text/x-python', for example, if we can detect the source code type
# If we can't and we know that it's plaintext, we'll use 'text/plain'

# The first goal is to detect files via inspection so that we aren't dependent on
# extensions, and so that we can catch naughty files before we even try to process them
# The second goal is to hopefully have support for far more media types. This should
# allow us to broaden our supported media types to support basically everything.
# We need to convert uploaded media to browser-friendly formats. We can do videos and
# audio with ffmpeg, and we can do images with imagemagick.

# Returns:
# {
#   'type': 'video' | 'audio' | 'image' | full mimetype,
#   'extra': { }, # type-specific extra data for the processor
#   'flags': { 'autoplay': True, 'loop': True, 'mute': False } # type-specific
# }
def detect(path):
    result = detect_ffprobe(path)
    if result != None:
        return result
    # ffprobe can't identify images without examining the extensions, and doesn't
    # support SVG at all
    # Note that ffprobe *can* confirm the integrity of images if it knows the extension
    # first, so we allow it to resolve images if the provided extension makes sense.
    result = detect_imagemagick(path)
    if result != None:
        return result
    result = detect_plaintext(path)
    if result != None:
        return result
    return None

# This does *not* work with any containers that only have images in them, by design.
def detect_ffprobe(path):
    # IMPORTANT: jdiez, this doesn't work when the path has spaces in it
    # I tried wrapping {0} in quotes to no avail
    a = Invocation('ffprobe -print_format json -loglevel quiet -show_format -show_streams {0}')
    a(path)
    a.run()
    if a.returncode or a.exited:
        return None
    result = json.loads(a.stdout[0])
    if result["format"]["nb_streams"] == 1:
        return detect_stream(result["streams"][0])
    # Try to guess what it is from the streams inside
    # I've done a little more detection than we really need to, for things like subtitles
    audio_streams = 0
    video_streams = 0
    image_streams = 0
    subtitle_streams = 0
    font_streams = 0
    # We shouldn't penalize people for unknown streams, I just figured we could make a note of it
    unknown_streams = 0

    for stream in result["streams"]:
        s = detect_stream(stream)
        t = s['type']
        if not s or not t:
            unknown_streams += 1
        else:
            if t.startswith('image'):
                image_streams += 1
            elif t == 'video':
                video_streams += 1
            elif t == 'audio':
                audio_streams += 1
            elif t == 'subtitle':
                subtitle_streams += 1
            elif t == 'font':
                font_streams += 1
            else:
                unknown_streams += 1
    if audio_streams == 1 and video_streams == 0:
        return {
            'type': 'audio',
            'extra': { 'has_audio': True, 'has_video': False },
            'flags': None
        }
    if video_streams > 0:
        return {
            'type': 'video',
            'extra': { 'has_audio': audio_streams > 0, 'has_video': True },
            'flags': {
                'autoplay': False,
                'loop': False,
                'mute': False,
            }
        }
    return None

def detect_stream(stream):
    # This will return None for things it doesn't recognize, or:
    # 'image/whatever' (uses full mimetype for images)
    # 'video'
    # 'audio'
    # 'subtitle'
    # 'font'
    if not "codec_name" in stream:
        if "tags" in stream and "mimetype" in stream["tags"]:
            if stream["tags"]["mimetype"] == 'application/x-truetype-font':
                return {
                    'type': 'font',
                    'extra': stream["tags"]["filename"],
                    'flags': None
                }
    else:
        if stream["codec_name"] == 'mjpeg':
            return {
                'type': 'image/jpeg',
                'extra': None,
                'flags': None
            }
        if stream["codec_name"] == 'png':
            return {
                'type': 'image/png',
                'extra': None,
                'flags': None
            }
        if stream["codec_name"] == 'bmp':
            return None
        if stream["codec_name"] == 'gif':
            return {
                'type': 'video',
                'extra': { 'has_audio': False, 'has_video': True },
                'flags': {
                    'autoplay': True,
                    'loop': True,
                    'mute': True
                }
            }
    if stream["codec_type"] == 'video':
        return {
            'type': 'video',
            'extra': { 'has_audio': False, 'has_video': True },
            'flags': {
                'autoplay': False,
                'loop': False,
                'mute': False
            }
        }
    if stream["codec_type"] == 'audio':
        return {
            'type': 'audio',
            'extra': { 'has_audio': True, 'has_video': False },
            'flags': {
                'autoplay': False,
                'loop': False,
                'mute': False
            }
        }
    if stream["codec_type"] == 'subtitle':
        return {
            'type': 'subtitle',
            'extra': { 'codec_name': stream['codec_name'] },
            'flags': None
        }
    return None

def detect_imagemagick(path):
    a = Invocation('identify -verbose {0}')
    a(path)
    a.run()
    if a.returncode or a.exited:
        return None
    result = a.stdout[0].split('\n')
    # Check for an actual mimetype first
    mimetype = None
    for line in result:
        line = line.lstrip(' ')
        if line.startswith('Mime type: '):
            mimetype = line[11:]
    if mimetype in [ 'image/png', 'image/jpeg' ]:
        return {
            'type': mimetype,
            'extra': None,
            'flags': None
        }
    # Check for SVG, it's special
    for line in result:
        line = line.lstrip(' ')
        if line ==  'Format: SVG (Scalable Vector Graphics)':
            return {
                'type': 'image/svg+xml',
                'extra': None,
                'flags': None
            }

    return {
        'type': 'image',
        'extra': None,
        'flags': None
    }

def detect_plaintext(path):
    a = Invocation('file -b -e elf -e tar -e compress -e cdf -e apptype -i {0}')
    a(path)
    a.run()
    if a.returncode or a.exited:
        return None
    result = a.stdout[0]
    if result.startswith('text/x-') or result == 'text/plain':
        return {
            'type': result[:result.find(';')],
            'extra': None,
            'flags': None
        }
    return None

class BitVector(object):
    shifts = {}
    _vec = 0

    def __init__(self, names, iv=0):
        for i, name in enumerate(names):
            self.shifts[name] = i

        self._vec = iv

    def __getattr__(self, name):
        if name not in self.shifts:
            raise AttributeError(name)

        value = self._vec & (1 << self.shifts[name])
        return True if value != 0 else False

    def __setattr__(self, name, v):
        if name == '_vec':
            object.__setattr__(self, '_vec', v)
            return

        if name not in self.shifts:
            raise AttributeError(name)

        newvec = self._vec

        currentval = getattr(self, name)
        if currentval == v:
            return # No change needed

        if currentval == True:
            # Turn this bit off
            newvec &= ~(1 << self.shifts[name])
        else:
            # Turn it on
            newvec |= (1 << self.shifts[name])

        object.__setattr__(self, '_vec', newvec)

    def as_dict(self):
        return dict((flag, getattr(self, flag)) for flag in self.shifts)

    def __int__(self):
        return self._vec

flags_per_processor = {
    'video': ['autoplay', 'loop', 'mute']
}

if __name__ == '__main__':
    files = File.get_all()
    count = len(files)

    print "About to process %d files." % count

    done = 0
    errors = []

    for f in files:
        h = f.hash
        configvector = 0

        try:
            path = file_storage(f.original)
            result = detect(path)

            if result and result['flags']:
                bv = BitVector(flags_per_processor.get(result['type'], []))

                for flag, value in result['flags'].items():
                    setattr(bv, flag, value)

                configvector = int(bv)
                print h, result['type'], int(bv)
            done += 1
        except Exception, e:
            errors.append(h)

        k = _k("file.%s" % h)
        r.hset(k, "configvector", configvector)

    print "%d/%d files processed, errors:" % (done, count), errors
