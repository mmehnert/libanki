# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import time, datetime, simplejson, random, itertools
from operator import itemgetter
from heapq import *
#from anki.cards import Card
from anki.utils import parseTags, ids2str, intTime, fmtTimeSpan
from anki.lang import _, ngettext
from anki.consts import *
from anki.hooks import runHook

# revlog:
# types: 0=lrn, 1=rev, 2=relrn, 3=cram
# positive intervals are in days (rev), negative intervals in seconds (lrn)

# the standard Anki scheduler
class Scheduler(object):
    name = "std"
    def __init__(self, deck):
        self.deck = deck
        self.queueLimit = 200
        self.reportLimit = 1000
        self.reps = 0
        self._updateCutoff()

    def getCard(self):
        "Pop the next card from the queue. None if finished."
        self._checkDay()
        id = self._getCardId()
        if id:
            c = self.deck.getCard(id)
            c.startTimer()
            return c

    def reset(self):
        self._resetConf()
        self._resetCounts()
        self._resetLrn()
        self._resetRev()
        self._resetNew()

    def answerCard(self, card, ease):
        assert ease >= 1 and ease <= 4
        self.deck.markReview(card)
        self.reps += 1
        card.reps += 1
        if card.queue == 0:
            # put it in the learn queue
            card.queue = 1
            card.type = 1
        if card.queue == 1:
            self._answerLrnCard(card, ease)
        elif card.queue == 2:
            self._answerRevCard(card, ease)
        else:
            card.queue = 1
            raise Exception("Invalid queue: "+str(card.queue))
        card.mod = intTime()
        card.flushSched()

    def counts(self):
        "Does not include fetched but unanswered."
        return (self.newCount, self.lrnCount, self.revCount)

    def dueForecast(self, days=7):
        "Return counts over next DAYS. Includes today."
        daysd = dict(self.deck.db.all("""
select due, count() from cards
where queue = 2 %s
and due between ? and ?
group by due
order by due""" % self._groupLimit(),
                            self.today,
                            self.today+days-1))
        for d in range(days):
            d = self.today+d
            if d not in daysd:
                daysd[d] = 0
        # return in sorted order
        ret = [x[1] for x in sorted(daysd.items())]
        return ret

    def countIdx(self, card):
        return card.queue

    def answerButtons(self, card):
        if card.queue == 2:
            return 4
        else:
            return 3

    def onClose(self):
        "Unbury and remove temporary suspends on close."
        self.deck.db.execute(
            "update cards set queue = type where queue between -3 and -2")

    def etaStr(self):
        eta = self.eta()
        if not eta:
            return ""
        return fmtTimeSpan(eta)

    def eta(self):
        "A very rough estimate of time to review."
        (cnt, sum) = self.deck.db.first("""
select count(), sum(taken) from (select * from revlog
order by time desc limit 10)""")
        if not cnt:
            return 0
        avg = sum / float(cnt)
        c = self.counts()
        # Here we just assume new/lrn will require 3x the number of reviews.
        # To improve on this we'll need to make grade count down so we can get
        # a decent picture of required steps.
        return (avg*c[0]*3 + avg*c[1]*3 + avg*c[2]) / 1000.0

    # Counts
    ##########################################################################

    def selCounts(self):
        "Return counts for selected groups, without building queue."
        self._resetCounts()
        return self.counts()

    def allCounts(self):
        "Return counts for all groups, without building queue."
        conf = self.deck.qconf['groups']
        if conf:
            self.deck.qconf['groups'] = []
            self._resetCounts()
            self.deck.qconf['groups'] = conf
        return self.counts()

    def _resetCounts(self):
        self._updateCutoff()
        self._resetLrnCount()
        self._resetRevCount()
        self._resetNewCount()

    # Group counts
    ##########################################################################

    def groupCounts(self):
        "Returns [groupname, cards, due, new]"
        gids = {}
        for (gid, all, rev, new) in self.deck.db.execute("""
select gid, count(),
sum(case when queue = 2 and due <= ? then 1 else 0 end),
sum(case when queue = 0 then 1 else 0 end)
from cards group by gid""", self.today):
            gids[gid] = [all, rev, new]
        return [[name, gid]+gids.get(gid, [0, 0, 0]) for (gid, name) in
                self.deck.db.execute(
                    "select id, name from groups order by name")]

    def groupCountTree(self):
        return self._groupChildren(self.groupCounts())

    def groupTree(self):
        "Like the count tree without the counts. Faster."
        return self._groupChildren([[name, gid, 0, 0, 0] for (gid, name) in
                self.deck.db.execute(
                    "select id, name from groups order by name")])

    def _groupChildren(self, grps):
        tree = []
        # split strings
        for g in grps:
            g[0] = g[0].split("::", 1)
        # group and recurse
        def key(grp):
            return grp[0][0]
        for (head, tail) in itertools.groupby(grps, key=key):
            tail = list(tail)
            gid = None
            all = 0
            rev = 0
            new = 0
            children = []
            for c in tail:
                if len(c[0]) == 1:
                    # current node
                    gid = c[1]
                    all += c[2]
                    rev += c[3]
                    new += c[4]
                else:
                    # set new string to tail
                    c[0] = c[0][1]
                    children.append(c)
            children = self._groupChildren(children)
            # tally up children counts
            for ch in children:
                all += ch[2]
                rev += ch[3]
                new += ch[4]
            tree.append((head, gid, all, rev, new, children))
        return tuple(tree)

    # Getting the next card
    ##########################################################################

    def _getCardId(self):
        "Return the next due card id, or None."
        # learning card due?
        id = self._getLrnCard()
        if id:
            return id
        # new first, or time for one?
        if self._timeForNewCard():
            return self._getNewCard()
        # card due for review?
        id = self._getRevCard()
        if id:
            return id
        # new cards left?
        id = self._getNewCard()
        if id:
            return id
        # collapse or finish
        return self._getLrnCard(collapse=True)

    # New cards
    ##########################################################################

    # FIXME: need to keep track of reps for timebox and new card introduction

    def _resetNewCount(self):
        l = self.deck.qconf
        if l['newToday'][0] != self.today:
            # it's a new day; reset counts
            l['newToday'] = [self.today, 0]
        lim = min(self.reportLimit, l['newPerDay'] - l['newToday'][1])
        if lim <= 0:
            self.newCount = 0
        else:
            self.newCount = self.deck.db.scalar("""
select count() from (select id from cards where
queue = 0 %s limit %d)""" % (self._groupLimit(), lim))

    def _resetNew(self):
        lim = min(self.queueLimit, self.newCount)
        self.newQueue = self.deck.db.all("""
select id, due from cards where
queue = 0 %s order by due limit %d""" % (self._groupLimit(),
                                         lim))
        self.newQueue.reverse()
        self._updateNewCardRatio()

    def _getNewCard(self):
        # We rely on sqlite to return the cards in id order. This may not
        # correspond to the 'ord' order. The alternative would be to do
        # something like due = fid*100+ord, but then we have no efficient way
        # of spacing siblings as we'd need to fetch the fid as well.
        if self.newQueue:
            (id, due) = self.newQueue.pop()
            # move any siblings to the end?
            if self.deck.qconf['newTodayOrder'] == NEW_TODAY_ORD:
                n = len(self.newQueue)
                while self.newQueue and self.newQueue[-1][1] == due:
                    self.newQueue.insert(0, self.newQueue.pop())
                    n -= 1
                    if not n:
                        # we only have one fact in the queue; stop rotating
                        break
            self.newCount -= 1
            return id

    def _updateNewCardRatio(self):
        if self.deck.qconf['newSpread'] == NEW_CARDS_DISTRIBUTE:
            if self.newCount:
                self.newCardModulus = (
                    (self.newCount + self.revCount) / self.newCount)
                # if there are cards to review, ensure modulo >= 2
                if self.revCount:
                    self.newCardModulus = max(2, self.newCardModulus)
                return
        self.newCardModulus = 0

    def _timeForNewCard(self):
        "True if it's time to display a new card when distributing."
        if not self.newCount:
            return False
        if self.deck.qconf['newSpread'] == NEW_CARDS_LAST:
            return False
        elif self.deck.qconf['newSpread'] == NEW_CARDS_FIRST:
            return True
        elif self.newCardModulus:
            return self.reps and self.reps % self.newCardModulus == 0

    # Learning queue
    ##########################################################################

    def _resetLrnCount(self):
        self.lrnCount = self.deck.db.scalar("""
select count() from (select id from cards where
queue = 1 %s and due < ? limit %d)""" % (
            self._groupLimit(), self.reportLimit),
            intTime() + self.deck.qconf['collapseTime'])

    def _resetLrn(self):
        self.lrnQueue = self.deck.db.all("""
select due, id from cards where
queue = 1 %s and due < :lim order by due
limit %d""" % (self._groupLimit(), self.reportLimit), lim=self.dayCutoff)

    def _getLrnCard(self, collapse=False):
        if self.lrnQueue:
            cutoff = time.time()
            if collapse:
                cutoff += self.deck.qconf['collapseTime']
            if self.lrnQueue[0][0] < cutoff:
                id = heappop(self.lrnQueue)[1]
                self.lrnCount -= 1
                return id

    def _answerLrnCard(self, card, ease):
        # ease 1=no, 2=yes, 3=remove
        conf = self._lrnConf(card)
        if card.type == 2:
            type = 2
        else:
            type = 0
        leaving = False
        if ease == 3:
            self._rescheduleAsRev(card, conf, True)
            leaving = True
        elif ease == 2 and card.grade+1 >= len(conf['delays']):
            self._rescheduleAsRev(card, conf, False)
            leaving = True
        else:
            card.cycles += 1
            if ease == 2:
                card.grade += 1
            else:
                card.grade = 0
            delay = self._delayForGrade(conf, card.grade)
            if card.due < time.time():
                # not collapsed; add some randomness
                delay *= random.uniform(1, 1.25)
            card.due = int(time.time() + delay)
            heappush(self.lrnQueue, (card.due, card.id))
            # if it's due within the cutoff, increment count
            if delay <= self.deck.qconf['collapseTime']:
                self.lrnCount += 1
        self._logLrn(card, ease, conf, leaving, type)

    def _delayForGrade(self, conf, grade):
        try:
            delay = conf['delays'][grade]
        except IndexError:
            delay = conf['delays'][-1]
        return delay*60

    def _lrnConf(self, card):
        conf = self._cardConf(card)
        if card.type == 2:
            return conf['lapse']
        else:
            return conf['new']

    def _rescheduleAsRev(self, card, conf, early):
        if card.type == 2:
            # failed; put back entry due
            card.due = card.edue
        else:
            self._rescheduleNew(card, conf, early)
        card.queue = 2
        card.type = 2

    def _graduatingIvl(self, card, conf, early):
        if card.type == 2:
            # lapsed card being relearnt
            return card.ivl
        if not early:
            # graduate
            ideal =  conf['ints'][0]
        elif card.cycles:
            # remove
            ideal = conf['ints'][2]
        else:
            # first time bonus
            ideal = conf['ints'][1]
        return self._adjRevIvl(card, ideal)

    def _rescheduleNew(self, card, conf, early):
        card.ivl = self._graduatingIvl(card, conf, early)
        card.due = self.today+card.ivl
        card.factor = conf['initialFactor']

    def _logLrn(self, card, ease, conf, leaving, type):
        # limit time taken to global setting
        taken = min(card.timeTaken(), self._cardConf(card)['maxTaken']*1000)
        lastIvl = -(self._delayForGrade(conf, max(0, card.grade-1)))
        ivl = card.ivl if leaving else -(self._delayForGrade(conf, card.grade))
        def log():
            self.deck.db.execute(
                "insert into revlog values (?,?,?,?,?,?,?,?)",
                int(time.time()*1000), card.id, ease,
                ivl, lastIvl,
                card.factor, taken, type)
        try:
            log()
        except:
            # duplicate pk; retry in 10ms
            time.sleep(0.01)
            log()

    def removeFailed(self, ids=None):
        "Remove failed cards from the learning queue."
        extra = ""
        if ids:
            extra = " and id in "+ids2str(ids)
        self.deck.db.execute("""
update cards set
due = edue, queue = 2, mod = %d
where queue = 1 and type = 2
%s
""" % (intTime(), extra))

    # Reviews
    ##########################################################################

    def _resetRevCount(self):
        self.revCount = self.deck.db.scalar("""
select count() from (select id from cards where
queue = 2 %s and due <= :lim limit %d)""" % (
            self._groupLimit(), self.reportLimit),
                                       lim=self.today)

    def _resetRev(self):
        self.revQueue = self.deck.db.list("""
select id from cards where
queue = 2 %s and due <= :lim order by %s limit %d""" % (
            self._groupLimit(), self._revOrder(), self.queueLimit),
                                    lim=self.today)
        if self.deck.qconf['revOrder'] == REV_CARDS_RANDOM:
            r = random.Random()
            r.seed(self.today)
            r.shuffle(self.revQueue)
        else:
            self.revQueue.reverse()

    def _getRevCard(self):
        if self._haveRevCards():
            self.revCount -= 1
            return self.revQueue.pop()

    def _haveRevCards(self):
        if self.revCount:
            if not self.revQueue:
                self._resetRev()
            return self.revQueue

    def _revOrder(self):
        return ("ivl desc",
                "ivl",
                "due")[self.deck.qconf['revOrder']]

    # Answering a review card
    ##########################################################################

    def _answerRevCard(self, card, ease):
        if ease == 1:
            self._rescheduleLapse(card)
        else:
            self._rescheduleRev(card, ease)
        self._logRev(card, ease)

    def _rescheduleLapse(self, card):
        conf = self._cardConf(card)['lapse']
        card.lapses += 1
        card.lastIvl = card.ivl
        card.ivl = self._nextLapseIvl(card, conf)
        card.factor = max(1300, card.factor-200)
        card.due = self.today + card.ivl
        # put back in the learn queue?
        if conf['relearn']:
            card.edue = card.due
            card.due = int(self._delayForGrade(conf, 0) + time.time())
            card.queue = 1
            self.lrnCount += 1
            heappush(self.lrnQueue, (card.due, card.id))
        # leech?
        self._checkLeech(card, conf)

    def _nextLapseIvl(self, card, conf):
        return int(card.ivl*conf['mult']) + 1

    def _rescheduleRev(self, card, ease):
        # update interval
        card.lastIvl = card.ivl
        self._updateRevIvl(card, ease)
        # then the rest
        card.factor = max(1300, card.factor+[-150, 0, 150][ease-2])
        card.due = self.today + card.ivl

    def _logRev(self, card, ease):
        taken = min(card.timeTaken(), self._cardConf(card)['maxTaken']*1000)
        def log():
            self.deck.db.execute(
                "insert into revlog values (?,?,?,?,?,?,?,?)",
                int(time.time()*1000), card.id, ease,
                card.ivl, card.lastIvl, card.factor, taken,
                1)
        try:
            log()
        except:
            # duplicate pk; retry in 10ms
            time.sleep(0.01)
            log()

    # Interval management
    ##########################################################################

    def _nextRevIvl(self, card, ease):
        "Ideal next interval for CARD, given EASE."
        delay = self._daysLate(card)
        conf = self._cardConf(card)
        fct = card.factor / 1000.0
        if ease == 2:
            interval = (card.ivl + delay/4) * 1.2
        elif ease == 3:
            interval = (card.ivl + delay/2) * fct
        elif ease == 4:
            interval = (card.ivl + delay) * fct * conf['rev']['ease4']
        # must be at least one day greater than previous interval; two if easy
        return max(card.ivl + (2 if ease==4 else 1), int(interval))

    def _daysLate(self, card):
        "Number of days later than scheduled."
        return max(0, self.today - card.due)

    def _updateRevIvl(self, card, ease):
        "Update CARD's interval, trying to avoid siblings."
        idealIvl = self._nextRevIvl(card, ease)
        card.ivl = self._adjRevIvl(card, idealIvl)

    def _adjRevIvl(self, card, idealIvl):
        "Given IDEALIVL, return an IVL away from siblings."
        idealDue = self.today + idealIvl
        conf = self._cardConf(card)['rev']
        # find sibling positions
        dues = self.deck.db.list(
            "select due from cards where fid = ? and queue = 2"
            " and id != ?", card.fid, card.id)
        if not dues or idealDue not in dues:
            return idealIvl
        else:
            leeway = max(conf['minSpace'], int(idealIvl * conf['fuzz']))
            fudge = 0
            # do we have any room to adjust the interval?
            if leeway:
                # loop through possible due dates for an empty one
                for diff in range(1, leeway+1):
                    # ensure we're due at least tomorrow
                    if idealDue - diff >= 1 and (idealDue - diff) not in dues:
                        fudge = -diff
                        break
                    elif (idealDue + diff) not in dues:
                        fudge = diff
                        break
            return idealIvl + fudge

    # Leeches
    ##########################################################################

    def _checkLeech(self, card, conf):
        "Leech handler. True if card was a leech."
        lf = conf['leechFails']
        if not lf:
            return
        # if over threshold or every half threshold reps after that
        if (lf >= card.lapses and
            (card.lapses-lf) % (max(lf/2, 1)) == 0):
            # add a leech tag
            f = card.fact()
            f.addTag("leech")
            f.flush()
            # handle
            a = conf['leechAction']
            if a == 0:
                self.suspendCards([card.id])
                card.queue = -1
            # notify UI
            runHook("leech", card)

    # Tools
    ##########################################################################

    def _resetConf(self):
        "Update group conf cache."
        self.groupConfs = dict(self.deck.db.all("select id, gcid from groups"))
        self.confCache = {}

    def _cardConf(self, card):
        id = self.groupConfs[card.gid]
        if id not in self.confCache:
            self.confCache[id] = self.deck.groupConf(id)
        return self.confCache[id]

    def _groupLimit(self):
        l = self.deck.qconf['groups']
        if not l:
            # everything
            return ""
        return " and gid in %s" % ids2str(l)

    # Daily cutoff
    ##########################################################################

    def _updateCutoff(self):
        # days since deck created
        self.today = int((time.time() - self.deck.crt) / 86400)
        # end of day cutoff
        self.dayCutoff = self.deck.crt + (self.today+1)*86400

    def _checkDay(self):
        # check if the day has rolled over
        if time.time() > self.dayCutoff:
            self.updateCutoff()
            self.reset()

    # Deck finished state
    ##########################################################################

    def finishedMsg(self):
        return (
            "<h1>"+_("Congratulations!")+"</h1>"+
            self._finishedSubtitle()+
            "<br><br>"+
            self._nextDueMsg())

    def _finishedSubtitle(self):
        if self.deck.qconf['groups']:
            return _("You have finished the selected groups for now.")
        else:
            return _("You have finished the deck for now.")

    def _nextDueMsg(self):
        line = []
        rev = self.revTomorrow() + self.lrnTomorrow()
        if rev:
            line.append(
                ngettext("There will be <b>%s review</b>.",
                         "There will be <b>%s reviews</b>.", rev) % rev)
        new = self.newTomorrow()
        if new:
            line.append(
                ngettext("There will be <b>%d new</b> card.",
                         "There will be <b>%d new</b> cards.", new) % new)
        if line:
            line.insert(0, _("At this time tomorrow:"))
            buf = "<br>".join(line)
        else:
            buf = _("No cards are due tomorrow.")
        buf = '<style>b { color: #00f; }</style>' + buf
        return buf

    def lrnTomorrow(self):
        "Number of cards in the learning queue due tomorrow."
        return self.deck.db.scalar(
            "select count() from cards where queue = 1 and due < ?",
            self.dayCutoff+86400)

    def revTomorrow(self):
        "Number of reviews due tomorrow."
        return self.deck.db.scalar(
            "select count() from cards where queue = 2 and due = ?"+
            self._groupLimit(),
            self.today+1)

    def newTomorrow(self):
        "Number of new cards tomorrow."
        lim = self.deck.qconf['newPerDay']
        return self.deck.db.scalar(
            "select count() from (select id from cards where "
            "queue = 0 %s limit %d)" % (self._groupLimit(), lim))

    # Next time reports
    ##########################################################################

    def nextIvlStr(self, card, ease, short=False):
        "Return the next interval for CARD as a string."
        return fmtTimeSpan(
            self.nextIvl(card, ease), short=short)

    def nextIvl(self, card, ease):
        "Return the next interval for CARD, in seconds."
        if card.queue in (0,1):
            return self._nextLrnIvl(card, ease)
        elif ease == 1:
            # lapsed
            conf = self._cardConf(card)['lapse']
            if conf['relearn']:
                return conf['delays'][0]*60
            return self._nextLapseIvl(card, conf)*86400
        else:
            # review
            return self._nextRevIvl(card, ease)*86400

    # this isn't easily extracted from the learn code
    def _nextLrnIvl(self, card, ease):
        conf = self._lrnConf(card)
        if ease == 1:
            # grade 0
            return self._delayForGrade(conf, 0)
        elif ease == 3:
            # early removal
            return self._graduatingIvl(card, conf, True) * 86400
        else:
            grade = card.grade + 1
            if grade >= len(conf['delays']):
                # graduate
                return self._graduatingIvl(card, conf, False) * 86400
            else:
                # next level
                return self._delayForGrade(conf, grade)

    # Suspending
    ##########################################################################

    def suspendCards(self, ids):
        "Suspend cards."
        self.removeFailed(ids)
        self.deck.db.execute(
            "update cards set queue = -1, mod = ? where id in "+
            ids2str(ids), intTime())

    def unsuspendCards(self, ids):
        "Unsuspend cards."
        self.deck.db.execute(
            "update cards set queue = type, mod = ? "
            "where queue = -1 and id in "+ ids2str(ids),
            intTime())

    def buryFact(self, fid):
        "Bury all cards for fact until next session."
        self.deck.setDirty()
        self.removeFailed(
            self.deck.db.list("select id from cards where fid = ?", fid))
        self.deck.db.execute("update cards set queue = -2 where fid = ?", fid)

    # Counts
    ##########################################################################

    def timeToday(self):
        "Time spent learning today, in seconds."
        return self.deck.db.scalar(
            "select sum(taken/1000.0) from revlog where time > ?*1000",
            self.dayCutoff-86400) or 0

    def repsToday(self):
        "Number of cards answered today."
        return self.deck.db.scalar(
            "select count() from revlog where time > ?*1000",
            self.dayCutoff-86400)

    # Dynamic indices
    ##########################################################################

    def updateDynamicIndices(self):
        # determine required columns
        required = []
        if self.deck.qconf['revOrder'] in (
            REV_CARDS_OLD_FIRST, REV_CARDS_NEW_FIRST):
            required.append("interval")
        cols = ["queue", "due", "gid"] + required
        # update if changed
        if self.deck.db.scalar(
            "select 1 from sqlite_master where name = 'ix_cards_multi'"):
            rows = self.deck.db.all("pragma index_info('ix_cards_multi')")
        else:
            rows = None
        if not (rows and cols == [r[2] for r in rows]):
            self.deck.db.execute("drop index if exists ix_cards_multi")
            self.deck.db.execute("create index ix_cards_multi on cards (%s)" %
                              ", ".join(cols))
            self.deck.db.execute("analyze")

    # Resetting
    ##########################################################################

    def forgetCards(self, ids):
        "Put cards at the end of the new queue."
        self.deck.db.execute(
            "update cards set type=0, queue=0, ivl=0 where id in "+ids2str(ids))
        pmax = self.deck.db.scalar("select max(due) from cards where type=0")
        self.sortCards(ids, start=pmax+1, shuffle=self.deck.randomNew())

    def reschedCards(self, ids, imin, imax):
        "Put cards in review queue with a new interval in days (min, max)."
        d = []
        t = self.today
        mod = intTime()
        for id in ids:
            r = random.randint(imin, imax)
            d.append(dict(id=id, due=r+t, ivl=max(1, r), mod=mod))
        self.deck.db.executemany(
            "update cards set type=2,queue=2,ivl=:ivl,due=:due where id=:id",
            d)

    # Repositioning new cards
    ##########################################################################

    def sortCards(self, cids, start=1, step=1, shuffle=False, shift=False):
        scids = ids2str(cids)
        now = intTime()
        fids = self.deck.db.list(
            ("select distinct fid from cards where type = 0 and id in %s "
             "order by fid") % scids)
        # determine fid ordering
        due = {}
        if shuffle:
            random.shuffle(fids)
        for c, fid in enumerate(fids):
            due[fid] = start+c*step
        high = start+c*step
        # shift?
        if shift:
            low = self.deck.db.scalar(
                "select min(due) from cards where due >= ? and type = 0 "
                "and id not in %s" % scids,
                start)
            if low is not None:
                shiftby = high - low + 1
                self.deck.db.execute("""
update cards set mod=?, due=due+? where id not in %s
and due >= ?""" % scids, now, shiftby, low)
        # reorder cards
        d = []
        for id, fid in self.deck.db.execute(
            "select id, fid from cards where type = 0 and id in "+scids):
            d.append(dict(now=now, due=due[fid], cid=id))
        self.deck.db.executemany(
            "update cards set due = :due, mod = :now where id = :cid""", d)

    def randomizeCards(self):
        self.sortCards(self.deck.db.list("select id from cards"), shuffle=True)

    def orderCards(self):
        self.sortCards(self.deck.db.list("select id from cards"))
