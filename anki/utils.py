# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import re, os, random, time, types, math, htmlentitydefs, \
    tempfile, shutil
try:
    import subprocess
except ImportError:
    pass

try:
    import hashlib
    md5 = hashlib.md5
except ImportError:
    import md5
    md5 = md5.new

from anki.lang import _, ngettext
import locale, sys

if sys.version_info[1] < 5:
    def format_string(a, b):
        return a % b
    locale.format_string = format_string

# Time handling
##############################################################################

def intTime():
    return int(time.time())

timeTable = {
    "years": lambda n: ngettext("%s year", "%s years", n),
    "months": lambda n: ngettext("%s month", "%s months", n),
    "days": lambda n: ngettext("%s day", "%s days", n),
    "hours": lambda n: ngettext("%s hour", "%s hours", n),
    "minutes": lambda n: ngettext("%s minute", "%s minutes", n),
    "seconds": lambda n: ngettext("%s second", "%s seconds", n),
    }

afterTimeTable = {
    "years": lambda n: ngettext("%s year<!--after-->", "%s years<!--after-->", n),
    "months": lambda n: ngettext("%s month<!--after-->", "%s months<!--after-->", n),
    "days": lambda n: ngettext("%s day<!--after-->", "%s days<!--after-->", n),
    "hours": lambda n: ngettext("%s hour<!--after-->", "%s hours<!--after-->", n),
    "minutes": lambda n: ngettext("%s minute<!--after-->", "%s minutes<!--after-->", n),
    "seconds": lambda n: ngettext("%s second<!--after-->", "%s seconds<!--after-->", n),
    }

shortTimeTable = {
    "years": _("%sy"),
    "months": _("%smo"),
    "days": _("%sd"),
    "hours": _("%sh"),
    "minutes": _("%sm"),
    "seconds": _("%ss"),
    }

def fmtTimeSpan(time, pad=0, point=0, short=False, after=False, unit=99):
    "Return a string representing a time span (eg '2 days')."
    (type, point) = optimalPeriod(time, point, unit)
    time = convertSecondsTo(time, type)
    if not point:
        time = math.floor(time)
    if short:
        fmt = shortTimeTable[type]
    else:
        if after:
            fmt = afterTimeTable[type](_pluralCount(time, point))
        else:
            fmt = timeTable[type](_pluralCount(time, point))
    timestr = "%(a)d.%(b)df" % {'a': pad, 'b': point}
    return locale.format_string("%" + (fmt % timestr), time)

def optimalPeriod(time, point, unit):
    if abs(time) < 60 or unit < 1:
        type = "seconds"
        point -= 1
    elif abs(time) < 3599 or unit < 2:
        type = "minutes"
    elif abs(time) < 60 * 60 * 24 or unit < 3:
        type = "hours"
    elif abs(time) < 60 * 60 * 24 * 30 or unit < 4:
        type = "days"
    elif abs(time) < 60 * 60 * 24 * 365 or unit < 5:
        type = "months"
        point += 1
    else:
        type = "years"
        point += 1
    return (type, max(point, 0))

def convertSecondsTo(seconds, type):
    if type == "seconds":
        return seconds
    elif type == "minutes":
        return seconds / 60.0
    elif type == "hours":
        return seconds / 3600.0
    elif type == "days":
        return seconds / 86400.0
    elif type == "months":
        return seconds / 2592000.0
    elif type == "years":
        return seconds / 31536000.0
    assert False

def _pluralCount(time, point):
    if point:
        return 2
    return math.floor(time)

# Locale
##############################################################################

def fmtPercentage(float_value, point=1):
    "Return float with percentage sign"
    fmt = '%' + "0.%(b)df" % {'b': point}
    return locale.format_string(fmt, float_value) + "%"

def fmtFloat(float_value, point=1):
    "Return a string with decimal separator according to current locale"
    fmt = '%' + "0.%(b)df" % {'b': point}
    return locale.format_string(fmt, float_value)

# HTML
##############################################################################

def stripHTML(s):
    s = re.sub("(?s)<style.*?>.*?</style>", "", s)
    s = re.sub("(?s)<script.*?>.*?</script>", "", s)
    s = re.sub("<.*?>", "", s)
    s = entsToTxt(s)
    return s

def stripHTMLMedia(s):
    "Strip HTML but keep media filenames"
    s = re.sub("<img src=[\"']?([^\"'>]+)[\"']? ?/?>", " \\1 ", s)
    return stripHTML(s)

def minimizeHTML(s):
    "Correct Qt's verbose bold/underline/etc."
    s = re.sub('<span style="font-weight:600;">(.*?)</span>', '<b>\\1</b>',
               s)
    s = re.sub('<span style="font-style:italic;">(.*?)</span>', '<i>\\1</i>',
               s)
    s = re.sub('<span style="text-decoration: underline;">(.*?)</span>',
               '<u>\\1</u>', s)
    return s

def entsToTxt(html):
    def fixup(m):
        text = m.group(0)
        if text[:2] == "&#":
            # character reference
            try:
                if text[:3] == "&#x":
                    return unichr(int(text[3:-1], 16))
                else:
                    return unichr(int(text[2:-1]))
            except ValueError:
                pass
        else:
            # named entity
            try:
                text = unichr(htmlentitydefs.name2codepoint[text[1:-1]])
            except KeyError:
                pass
        return text # leave as is
    return re.sub("&#?\w+;", fixup, html)

# IDs
##############################################################################

def hexifyID(id):
    return "%x" % id

def dehexifyID(id):
    return int(id, 16)

def ids2str(ids):
    """Given a list of integers, return a string '(int1,int2,...)'."""
    return "(%s)" % ",".join(str(i) for i in ids)

# Tags
##############################################################################

def parseTags(tags):
    "Parse a string and return a list of tags."
    return [t for t in tags.split(" ") if t]

def joinTags(tags):
    "Join tags into a single string, with leading and trailing spaces."
    if not tags:
        return u""
    return u" %s " % u" ".join(tags)

def canonifyTags(tags):
    "Strip leading/trailing/superfluous spaces and duplicates."
    tags = [t.lstrip(":") for t in set(tags)]
    return joinTags(sorted(tags))

def hasTag(tag, tags):
    "True if TAG is in TAGS. Ignore case."
    return tag.lower() in [t.lower() for t in tags]

def addTags(addtags, tags):
    "Add tags if they don't exist."
    currentTags = parseTags(tags)
    for tag in parseTags(addtags):
        if not hasTag(tag, currentTags):
            currentTags.append(tag)
    return canonifyTags(currentTags)

def delTags(deltags, tags):
    "Delete tags if they don't exists."
    currentTags = parseTags(tags)
    for tag in parseTags(deltags):
        # find tags, ignoring case
        remove = []
        for tx in currentTags:
            if tag.lower() == tx.lower():
                remove.append(tx)
        # remove them
        for r in remove:
            currentTags.remove(r)
    return canonifyTags(currentTags)

# Fields
##############################################################################

def joinFields(list):
    return "\x1f".join(list)

def splitFields(string):
    return string.split("\x1f")

# Checksums
##############################################################################

def checksum(data):
    return md5(data).hexdigest()

def fieldChecksum(data):
    # 32 bit unsigned number from first 8 digits of md5 hash
    return int(checksum(data.encode("utf-8"))[:8], 16)

# Temp files
##############################################################################

_tmpdir = None

def tmpdir():
    "A reusable temp folder which we clean out on each program invocation."
    global _tmpdir
    if not _tmpdir:
        def cleanup():
            shutil.rmtree(_tmpdir)
        import atexit
        atexit.register(cleanup)
        _tmpdir = os.path.join(tempfile.gettempdir(), "anki_temp")
        try:
            shutil.rmtree(_tmpdir)
        except (IOError, OSError):
            pass
        os.mkdir(_tmpdir)
    return _tmpdir

def tmpfile(prefix=None, suffix=None):
    return tempfile.mkstemp(dir=tmpdir(), prefix=prefix, suffix=suffix)

def namedtmp(name):
    "Return tmpdir+name. Deletes any existing file."
    path = os.path.join(tmpdir(), name)
    try:
        os.unlink(path)
    except (OSError, IOError):
        pass
    return path

# Cmd invocation
##############################################################################

def call(argv, wait=True, **kwargs):
    "Execute a command. If WAIT, return exit code."
    # ensure we don't open a separate window for forking process on windows
    if isWin:
        si = subprocess.STARTUPINFO()
        try:
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        except:
            si.dwFlags |= subprocess._subprocess.STARTF_USESHOWWINDOW
    else:
        si = None
    # run
    try:
        o = subprocess.Popen(argv, startupinfo=si, **kwargs)
    except OSError:
        # command not found
        return -1
    # wait for command to finish
    if wait:
        while 1:
            try:
                ret = o.wait()
            except OSError:
                # interrupted system call
                continue
            break
    else:
        ret = 0
    return ret

# OS helpers
##############################################################################

isMac = sys.platform.startswith("darwin")
isWin = sys.platform.startswith("win32")
