"""
This is initial code for a Nethack bot, written by Tim Newsome
<nuisance@casualhacker.net> in 2004. I've abandoned this code for now, but
maybe I'll pick it up again some day.

This code is not going to just run on your system, if only because the path to
nethack is hard-coded. If you can't make the code run, just play back the
included .tty files (using ttyplay) and pretend you did. If you do get the
code to run on your system, and are hacking on it somewhat productively,
please let me know. If at that stage you have any questions, I'll try to
answer them.

A few .tty files are included for your enjoyment. They're not all made with
exactly this code. I can't easily make new ones because ttyrec doesn't like
devfs, or something.

The bot uses a named pipe to tee Nethack's output into a ttyrec xterm as well
as this program. Then it explores the dungeon, charges every monster, and
prays when hungry. It'll likely throw an exception when it encounters
something it hasn't seen yet. ISTR that the last thing I was working on was
making the bot push boulders out of the way in case it can't get to the down
staircase. The bot will already search the level for hidden doors if that is
the case.

I abandoned the code in favor of rewriting in ocaml because I was getting
annoyed at python's lack of type checking. Specifically I really feel the need
for a type-checked enum thing. I've also wanted to learn ocaml for a while,
and this seemed like a project that would really let me get a feel for the
language. So far I like the type system, but not the lack of name spaces, and
definitely not the need for prototypes.
"""

from __future__ import generators

# Current development "plan":
# Get this code to the point where it can run through a full game without
# encountering any unexpected messages, and without getting stuck somewhere.

# Functions should look like this:
def template(self):
    # function returns non-zero when there is a situation that the current
    # function is not equipped to deal with (ie gnome with wand of death).
    exc_level = self.push_except_function(function)
    try:
	# do whatever this function does
	# do NOT use return
	pass
    except NetHackException, e:
	if e.level < exc_level:
	    raise
    self.pop_except_function()
    # failure is 0 for success, non-zero for failure.
    return failure

import os, sys, select
import pty
import string
import re
import random
import time
import traceback

import Queue, bisect

os.environ['TERM'] = 'vt100'
os.environ['NETHACKOPTIONS'] = "number_pad,!autopickup,!sparkle,!timed_delay," \
	"!tombstone,scores:0t/0a/1o"

class PriorityQueue(Queue.Queue):
    def _put(self, item):
        bisect.insort(self.queue, item)

def hex_string(string):
    ret = ""
    for c in string:
	if ord(c) > ord(' ') and ord(c) <= ord('~'):
	    ret = ret + " %s" % c
	else:
	    ret = ret + " %02x" % ord(c)
    return ret

class NetHackException(Exception):
    def __init__(self, player, level):
	self.player = player
	self.level = level

	while len(player.except_functions) > level + 1:
	    player.pop_except_function()

class ErrorException(Exception):
    def __init__(self, string):
	self.string = string

class RoutePlanException(ErrorException):
    pass

class Terminal:
    ATTRIB_REVERSE = 0x100
    def __init__(self, lines=25, columns=80):
	self.lines = lines
	self.columns = columns
	# bits 0-6 contain the ASCII character
	# higher bits are for attributes
	self.chars = [32] * lines * columns
	self.x = 0
	self.y = 0
	self.state = ""
	self.prompt = ""
	self.mode = 0

	self.actions = {}
	for i in range(ord(' '), ord('~')+1):
	    self.actions["%c" % i] = "print"
	self.actions["\x00"] = "ignore"	    # fill character
	self.actions["\x08"] = 'backspace'
	self.actions["\x0a"] = 'LF'
	self.actions["\x0d"] = 'CR'
	self.actions["\x1b(B"] = "ignore"    # character set stuff
	self.actions["\x1b)0"] = "ignore"
	self.actions["\x1b>"] = "ignore"    # keypad in numeric mode
	self.actions["\x1b[?1l"] = "ignore" # cursor key mode
	self.actions["\x1b[H"] = "home"
	self.actions["\x1b[J"] = "clear to end of screen"
	self.actions["\x1b[K"] = "clear to end of line"
	self.actions["\x1b[A"] = "up"
	self.actions["\x1b[B"] = "down"
	self.actions["\x1b[C"] = "right"
	self.actions["\x1b[D"] = "left"
	self.actions["\x1b[7m"] = "reverse"
	self.actions["\x1b[0m"] = "clear mode"
	self.actions["\x1by"] = "ignore"	# dunno what this does

    def process(self, string):
	for char in string:
	    self.state = self.state + char
	    if self.state in self.actions:
		action = self.actions[self.state]
		if action == "print":
		    self.prompt = self.prompt + char
		else:
		    self.prompt = ""

		if action == "print":
		    self.chars[self.x + self.y * self.columns] = \
			    self.mode | ord(char)
		    self.x = self.x + 1
		elif action == 'CR':
		    self.x = 0
		elif action == 'backspace':
		    self.x = self.x - 1
		elif action in ('LF', "down"):
		    self.y = self.y + 1
		elif action == "up":
		    self.y = self.y - 1
		elif action == "left":
		    self.x = self.x - 1
		elif action == "right":
		    self.x = self.x + 1
		elif action == 'ignore':
		    pass
		elif action == "home":
		    self.x = 0
		    self.y = 0
		elif action == "reverse":
		    self.mode = self.mode | self.ATTRIB_REVERSE
		elif action == "clear mode":
		    self.mode = 0
		elif action == "clear to end of screen":
		    for i in range(self.x + self.y * self.columns,
			    self.columns * self.lines):
			self.chars[i] = ord(' ')
		elif action == "clear to end of line":
		    for i in range(self.x + self.y * self.columns,
			    self.columns * (self.y + 1)):
			self.chars[i] = ord(' ')
		else:
		    raise Exception, "Unknown action: %s" % action
		self.state = ""
		if self.x >= self.columns:
		    self.x = 0
		    self.y = self.y + 1
		if self.y >= self.lines:
		    self.y = self.lines - 1
		continue

	    # move cursor to absolute position
	    match = re.match("\x1b\[(\d+);(\d+)H", self.state)
	    if not match is None:
		self.prompt = ""
		self.y = int(match.group(1)) - 1
		self.x = int(match.group(2)) - 1
		self.state = ""
		continue

	    if len(self.state) > 10:
		raise Exception, "Unknown control codes: %s" % \
			hex_string(self.state)

    def draw(self):
	#sys.stdout.write("\x1b[H")
	c = map(lambda x: "%c" % (x & 0x7f), self.chars)
	for l in range(self.lines):
	    print string.join(c[self.columns * l: self.columns * (l+1)], "")

    def readline(self, line):
	c = map(lambda x: "%c" % (x & 0x7f), 
		self.chars[self.columns * line: self.columns * (line+1)])
	return string.join(c, "")

    def readline_raw(self, line):
	return self.chars[self.columns * line: self.columns * (line+1)]

class NetHack:
    def __init__(self, out_fd):
	self.terminal = Terminal()
	#(nethack_in, nethack_out) = os.popen2(cmd)
	(self.pid, self.fd) = pty.fork()
	self.out_fd = out_fd
	if self.pid == 0:
	    os.system("/home/drz/nethack/game/nethack -u bottie -p val")
	    sys.exit(0)
	#print "Child with pid=%d, fd=%d" % (self.pid, self.fd)
	self.poll = select.poll()
	self.poll.register(self.fd, select.POLLIN | select.POLLPRI)
	self.prompt = ""
	self.messages = []
	self.running = 1
	self._unknown = '%'

    def unknown(self):
	if self._unknown == '|':
	    self._unknown = '%'
	else:
	    self._unknown = '|'
	self.cmd(self._unknown, 5, 0)
	while 1:
	    if self.messages[-1] == "Unknown command '%s'." % self._unknown:
		break
	    self.cmd("", 5, 0)
	self.messages = self.messages[:-1]

    # wait until it's the user's turn to input something
    def wait(self, timeout=1000):
	self.terminal.prompt = ""
	self.prompt = ""
	while self.running:
	    list = self.poll.poll(timeout)
	    if len(list) == 0:
		break
	    try:
		data = os.read(self.fd, 64)
	    except OSError:
		#print "all done!"
		self.running = 0
	    self.out_fd.write(data)
	    self.out_fd.flush()
	    for c in data:
		self.terminal.process(c)

		if re.match("(Hit space to continue:|.*--More--|"
			"\(end\)|.*\[[ynq]+]|Call .*:)", self.terminal.prompt):
		    break
		if re.match(".*throws", self.terminal.prompt):
		    #raise Exception, "throwing detected!"
		    # no longer necessary, since we turned off timed_delay
		    #timeout = 1000
		    pass

	self.prompt = self.terminal.prompt.strip()
	if len(self.prompt) < 5:
	    self.prompt = ""

    # send a raw string, and wait until it's our turn to provide more input
    def send(self, str, timeout=200):
	print "sending '%s'" % str
	os.write(self.fd, str)
	self.wait(timeout)

    def parse_list(self, listx, listy):
	#print "list:", listx, listy
	list = []
	for y in range(listy):
	    list_entry = self.terminal.readline(y)[listx:].strip()
	    list.append(list_entry)
	    #print "list_entry:", list_entry
	return list

    # send the command "str", and wait until more input is required, hitting
    # space for --More-- and so on, while keeping track of all the messages
    # nethack displays
    def cmd(self, str, timeout=200, reset=1):
	messages = []
	self.send(str, timeout)

	while self.running:
	    #print "prompt: '%s'" % self.prompt
	    # the first line on the screen ends in a colon
	    match = re.match("(\s{9}\s+)\w", self.terminal.readline(0))
	    if match:
		#print "calling list on line:", self.terminal.readline(0)
		#print "whitespace: '%s'" % match.group(1)
		#print "groups:", match.groups()
		messages = messages + \
			self.parse_list(len(match.group(1)), self.terminal.y)
		self.send(" ", timeout)

	    elif re.match(".*--More--", self.prompt):
		# read message until the --More--
		m = ""
		for l in range(0, 25):
		    line = self.terminal.readline(l).strip()
		    match = re.match("(.*)--More--", line)
		    if len(m) > 0:
			m = m + " "
		    if match:
			m = m + match.group(1)
			break
		    else:
			m = m + line
		messages.append(m)
		self.send(" ", timeout)

	    elif self.prompt == "(end)":
		#print "found (end)"
		self.parse_list(self.terminal.x - 5, self.terminal.y)
		self.send(" ", timeout)

	    elif re.match(".* \[ynq?]", self.prompt):
		break

	    elif re.match(".*:$", self.prompt):
		break

	    elif len(self.prompt) < 1:
		# ignore blank prompts
		messages.append(self.terminal.readline(0).strip())
		break

	    else:
		# unknown prompt means it's not quite done drawing yet
		#raise Exception, "Unknown prompt: \"%s\"" % self.prompt
		self.send("", timeout)

	messages2 = []
	for m in messages:
	    # split up the individual sentences in the message
	    sentence = ""
	    open = None
	    punctuation = 0
	    #print "message:", m
	    for c in m:
		if c in ('"'):
		    if open == c:
			open = None
		    elif open is None:
			open = c
		    sentence = sentence + c
		elif c in ('.', '!') and open is None:
		    sentence = sentence + c
		    punctuation = 1
		elif c in string.whitespace:
		    if punctuation:
			if len(sentence) > 1:
			    messages2.append(sentence)
			    sentence = ""
			punctuation = 0
		    if len(sentence) > 0:
			sentence = sentence + c
		else:
		    sentence = sentence + c
		    punctuation = 0
	    if len(sentence):
		#print "sentence:", sentence
		messages2.append(sentence)
	messages2 = map(string.strip, messages2)
	messages2 = filter(lambda x: len(x) > 0, messages2)
	if len(messages2):
	    print "messages2:", messages2
	if len(self.prompt):
	    print "prompt:", self.prompt
	if reset:
	    self.messages = messages2
	else:
	    self.messages = self.messages + messages2

	self.parse_status()

    def parse_status(self):
	# line 1
	line = self.terminal.readline(22).strip()
	match = re.match("^(.*) the (.*?)\s+St:(\S+) Dx:(\d+) Co:(\d+) "
		"In:(\d+) Wi:(\d+) Ch:(\d+)\s+(Lawful|Neutral|Chaotic)", line)
	if match is None:
	    print "Couldn't parse status line: %s" % line
	else:
	    if len(match.group(0)) < len(line):
		raise "Unparsed status: %s" % line
	    (self.name, self.title, self.strength, self.dexterity,
		    self.constitution, self.intelligence, self.wisdom,
		    self.charisma, self.alignment) = match.groups()
	    match1 = re.match("(\d+)/(\d+)", self.strength)
	    match2 = re.match("(\d+)/\*\*", self.strength)
	    if match1:
		self.strength = float(match1.group(1)) + float(match1.group(2)) / 100
	    elif match2:
		self.strength = float(match2.group(1)) + 1
	    else:
		self.strength = int(self.strength)
	    self.dexterity = int(self.dexterity)
	    self.constitution = int(self.constitution)
	    self.intelligence = int(self.intelligence)
	    self.wisdom = int(self.wisdom)
	    self.charisma = int(self.charisma)

	# line 2
	line = self.terminal.readline(23).strip()
	match = re.match("""
	    Dlvl:(?P<depth>\d+)
	    \s+\$:(?P<gold>\d)
	    \s+HP:(?P<hp>\d+)\((?P<max_hp>\d+)\)
	    \s+Pw:(?P<pw>\d+)\((?P<max_pw>\d+)\)
	    \s+AC:(?P<ac>\d+)
	    \s+
	    (Exp:(?P<exp>\d+) | HD:(?P<hd>\d+))
	    (?P<flags>(\s+\w+))*$""", line, re.VERBOSE)
	if match is None:
	    print "Couldn't parse status line: %s" % line
	else:
	    if len(match.group(0)) < len(line):
		raise "Unparsed status: %s" % line
	    self.depth = int(match.group('depth'))
	    self.gold = int(match.group('gold'))
	    self.hp = int(match.group('hp'))
	    self.max_hp = int(match.group('max_hp'))
	    self.pw = int(match.group('pw'))
	    self.max_pw = int(match.group('max_pw'))
	    self.ac = int(match.group('ac'))
	    if match.group('exp'):
		self.level = int(match.group('exp'))
		self.hd = None
	    else:
		self.level = None
		self.hd = int(match.group('hd'))

	    self.hunger = 0
	    self.blind = 0
	    self.stunned = 0
	    self.burdened = 0
	    self.hallucinating = 0
	    flags = match.group('flags') or ''
	    for status in string.split(flags):
		status = status.strip()
		if status == "Hungry":
		    self.hunger = 1
		elif status == "Weak":
		    self.hunger = 2
		elif status in ("Fainting", 'Fainted'):
		    self.hunger = 3
		elif status == "Starved":
		    self.hunger = 4
		elif status == "Blind":
		    self.blind = 1
		elif status == "Stun":
		    self.stunned = 1
		elif status == "Hallu":
		    self.hallucinating = 1
		elif status == "Burdened":
		    self.burdened = 1
		else:
		    raise "Unknown status: '%s'" % status

    def get_map(self):
	map = []
	for l in range(2, 21):
	    map = map + self.terminal.readline_raw(l)
	return map

    def mypos(self):
	return (self.terminal.x, self.terminal.y - 2)

    def mydepth(self):
	return self.depth

    def semicolon(self, x, y):
	self.cmd(";", 5)
	timeout = 10
	while (len(self.messages) < 1 or 
		self.messages[0] != "Pick an object.") and timeout > 0:
	    self.cmd("", 5)
	    timeout = timeout - 1
	if timeout <= 0:
	    return None

	# top 2 lines are messages
	y = y + 2
	vx = x - self.terminal.x
	vy = y - self.terminal.y

	str = ""
	if (vx > 0):
	    str = str + ("l" * (vx / 8))
	    str = str + ("6" * (vx % 8))
	elif (vx < 0):
	    str = str + ("h" * (-vx / 8))
	    str = str + ("4" * (-vx % 8))
	if (vy > 0):
	    str = str + ("j" * (vy / 8))
	    str = str + ("2" * (vy % 8))
	elif (y < self.terminal.y):
	    str = str + ("k" * (-vy / 8))
	    str = str + ("8" * (-vy % 8))
	str = str + "."
	self.cmd(str, 5)

	while 1:
	    if len(self.messages) > 0:
		if self.messages[0] == "Pick an object.":
		    self.messages = self.messages[1:]
		else:
		    break
	    else:
		self.cmd("", 5)
	description = self.messages[0]
	if re.match("a ghost or a dark part of a room", description):
	    char = " "
	else:
	    char = description[0]
	if re.match(".*Pick", description):
	    raise Exception, description
	match = re.match(".*\((.+)\)( \[.*])?$", description)
	if match:
	    description = match.group(1)
	return (char, description)

    DIRECTION_TABLE = {
	(-1,-1): "7",
	( 0,-1): "8",
	( 1,-1): "9",
	(-1, 0): "4",
	( 1, 0): "6",
	(-1, 1): "1",
	( 0, 1): "2",
	( 1, 1): "3"
	}

    # direction is given as a vector, ie (1,0) or (-1,1)
    def move(self, direction):
	if direction not in self.DIRECTION_TABLE:
	    raise Exception, "bad direction: %d,%d" % (direction[0], direction[1])
	cmd = self.DIRECTION_TABLE[direction]
	self.cmd(cmd, 70)
	#self.unknown()

    def open(self, direction):
	self.cmd("o", 70)
	for m in nethack.messages:
	    if m == "You can't open anything -- you have no hands!":
		return 1
	    elif m == 'In what direction?':
		pass
	    else:
		raise "Unexpected message: %s" % m
	self.cmd(self.DIRECTION_TABLE[direction], 70)
	return 0

    def fight(self, direction):
	cmd = "F" + self.DIRECTION_TABLE[direction]
	self.cmd(cmd, 70)

    def kick(self, direction):
	cmd = "\x04" + self.DIRECTION_TABLE[direction]
	self.cmd(cmd, 70)

    def pray(self):
	self.cmd("#p\n", 5)
	while len(self.messages) < 1 or \
		not re.match("Are you sure you want to pray?", self.messages[0]):
	    self.cmd("", 5)
	self.cmd("y", 70)

class NetHackCreature:
    def __init__(self, description):
	self.description = description
	if re.match("tame", description):
	    self.hostile = 0
	    self.tame = 1
	    self.peaceful = 0
	    self.me = 0
	elif re.match("peaceful", description):
	    self.hostile = 0
	    self.tame = 0
	    self.peaceful = 1
	    self.me = 0
	elif re.match(".* called bottie", description):
	    self.hostile = 0
	    self.tame = 0
	    self.peaceful = 0
	    self.me = 1
	else:
	    self.hostile = 1
	    self.tame = 0
	    self.peaceful = 0
	    self.me = 0

    def __str__(self):
	return self.description

class NetHackMapSquare:
    # haven't seen this square yet
    UNEXPLORED = 0
    FLOOR = 1
    WALL = 2
    OPEN_DOOR = 3
    BROKEN_DOOR = 4
    CLOSED_DOOR = 5
    CORRIDOR = 6
    DOORWAY = 7
    STAIRCASE_UP = 8
    STAIRCASE_DOWN = 9
    # don't know what's on this square because there's something on top of it
    UNKNOWN = 10
    # this is a floor/corridor whatever that you can walk on, but can't tell
    # what's there because there's an object on top of it
    UNKNOWN_PASSABLE = 11
    FOUNTAIN = 12
    SINK = 13
    GRAVE = 14
    PIT = 15
    ARROW_TRAP = 16
    NEUTRAL_ALTAR = 17
    CHAOTIC_ALTAR = 18
    LAWFUL_ALTAR = 19
    BEAR_TRAP = 20
    DART_TRAP = 21
    MAGIC_TRAP = 22
    RUST_TRAP = 23
    SQUEAKY_BOARD = 24
    LOCKED_DOOR = 25
    FALLING_ROCK_TRAP = 26
    ANTI_MAGIC_FIELD = 27
    FIRE_TRAP = 28
    HOLE = 29
    TELEPORT_TRAP = 30
    SLEEPING_GAS_TRAP = 31
    ROLLING_BOULDER_TRAP = 32
    TRAP_DOOR = 33
    TREE = 34
    LEVEL_TELEPORTER = 35
    WEB = 36

    # include unknowns because they might be doors
    # don't include unkowns because they might not be doors
    GRID_TERRAIN = (OPEN_DOOR,)
    # Include unknown here. We might later decide that this particular unknown
    # isn't passable after all, if we try moving onto it but it doesn't work.
    PASSABLE_TERRAIN = (OPEN_DOOR, FLOOR, CORRIDOR, DOORWAY, STAIRCASE_UP,
	    STAIRCASE_DOWN, UNKNOWN, UNKNOWN_PASSABLE, FOUNTAIN, SINK, GRAVE,
	    PIT, ARROW_TRAP, NEUTRAL_ALTAR, CHAOTIC_ALTAR, LAWFUL_ALTAR,
	    BEAR_TRAP, BROKEN_DOOR, DART_TRAP, MAGIC_TRAP, RUST_TRAP,
	    SQUEAKY_BOARD, FALLING_ROCK_TRAP, ANTI_MAGIC_FIELD, FIRE_TRAP,
	    HOLE, TELEPORT_TRAP, SLEEPING_GAS_TRAP, ROLLING_BOULDER_TRAP,
	    TRAP_DOOR, LEVEL_TELEPORTER, WEB)

    BOULDER = 0

    def draw_terrain(self):
	lookup = {
	    self.UNEXPLORED: ",",
	    self.FLOOR: ".",
	    self.WALL: "W",
	    self.OPEN_DOOR: "d",
	    self.BROKEN_DOOR: "b",
	    self.CLOSED_DOOR: "D",
	    self.LOCKED_DOOR: "D",
	    self.CORRIDOR: "#",
	    self.DOORWAY: ".",
	    self.STAIRCASE_UP: "<",
	    self.STAIRCASE_DOWN: ">",
	    self.UNKNOWN: ";",
	    self.UNKNOWN_PASSABLE: ":",
	    self.FOUNTAIN: "{",
	    self.SINK: "#",
	    self.GRAVE: "(",
	    self.PIT: "^",
	    self.ARROW_TRAP: "^",
	    self.NEUTRAL_ALTAR: "_",
	    self.CHAOTIC_ALTAR: "_",
	    self.LAWFUL_ALTAR: "_",
	    self.BEAR_TRAP: "^",
	    self.DART_TRAP: "^",
	    self.MAGIC_TRAP: "^",
	    self.RUST_TRAP: "^",
	    self.SQUEAKY_BOARD: "^",
	    self.FALLING_ROCK_TRAP: "^",
	    self.ANTI_MAGIC_FIELD: "^",
	    self.FIRE_TRAP: "^",
	    self.HOLE: "^",
	    self.TRAP_DOOR: "^",
	    self.TELEPORT_TRAP: "^",
	    self.SLEEPING_GAS_TRAP: "^",
	    self.ROLLING_BOULDER_TRAP: "^",
	    self.TREE: "#",
	    self.LEVEL_TELEPORTER: "^",
	    self.WEB: "^",
	}
	if self.terrain in lookup:
	    char = lookup[self.terrain]
	else:
	    char = '?'
	if not self.passable():
	    return "\x1b[31m%s\x1b[0m" % char
	else:
	    return char

    def travel_cost(self):
	table = {
	    self.UNEXPLORED: 1000,
	    self.FLOOR: 1,
	    self.WALL: 1000,
	    self.OPEN_DOOR: 1,
	    self.BROKEN_DOOR: 1,
	    self.CLOSED_DOOR: 5,
	    self.CORRIDOR: 1,
	    self.DOORWAY: 1,
	    self.STAIRCASE_UP: 1,
	    self.STAIRCASE_DOWN: 1,
	    self.UNKNOWN: 2,
	    self.UNKNOWN_PASSABLE: 2,
	    self.FOUNTAIN: 1,
	    self.SINK: 1,
	    self.GRAVE: 1,
	    self.PIT: 15,
	    self.ARROW_TRAP: 5,
	    self.NEUTRAL_ALTAR: 1,
	    self.CHAOTIC_ALTAR: 1,
	    self.LAWFUL_ALTAR: 1,
	    self.BEAR_TRAP: 15,
	    self.DART_TRAP: 10,
	    self.MAGIC_TRAP: 10,
	    self.RUST_TRAP: 20,
	    self.SQUEAKY_BOARD: 3,
	    self.LOCKED_DOOR: 8,
	    self.FALLING_ROCK_TRAP: 5,
	    self.ANTI_MAGIC_FIELD: 2,
	    self.FIRE_TRAP: 10,
	    self.HOLE: 100,
	    self.TRAP_DOOR: 100,
	    self.TELEPORT_TRAP: 100,
	    self.LEVEL_TELEPORTER: 200,
	    self.WEB: 6,
	    self.SLEEPING_GAS_TRAP: 40,
	    self.ROLLING_BOULDER_TRAP: 8,
	}
	cost = table[self.terrain]
	if self.creature:
	    if self.creature.peaceful:
		cost = cost + 10
	    elif self.creature.tame:
		cost = cost + 1
	    else:
		cost = cost + 5
	elif self.BOULDER in self.items:
	    cost = cost + 100
	# prefer squares that we've already visited/searched
	if self.visited > 0:
	    cost = cost - 0.1
	if self.searched > 0:
	    cost = cost - .01 * min(self.searched, 5)
	return cost

    def set_terrain(self, t):
	old = self.terrain
	if old != t:
	    self.map.changes = self.map.changes + 1
	    print "change terrain of", self, "to", t
	self.terrain = t
	if (old in self.GRID_TERRAIN) != (self.terrain in self.GRID_TERRAIN):
	    # update the square's neighbors
	    for x in range(self.x-1, self.x+2):
		for y in range(self.y-1, self.y+2):
		    square = self.map.get_square(x, y)
		    if square:
			square.update_neighbors()

    def __init__(self, map, x, y):
	self.items = None
	self.creature = None
	self.character = None
	self.mark = 0
	self.map = map
	self.x = x
	self.y = y
	self.visited = 0
	# how many times we've searched this location
	self.searched = 0

	self.terrain = self.UNEXPLORED
	self.items = []

    def update_neighbors(self):
	list = []
	if self.terrain in self.GRID_TERRAIN:
	    possible = ((-1,0), (0,-1), (1,0), (0,1))
	else:
	    possible = ((-1,0), (0,-1), (1,0), (0,1), (-1,-1), (-1,1), (1,-1),
		    (1,1))
	#print "update neighbors for", self, ":",
	for offset in possible:
	    x = self.x + offset[0]
	    y = self.y + offset[1]
	    neighbor = self.map.get_square(x, y)
	    if neighbor is None:
		continue
	    if neighbor.terrain in self.GRID_TERRAIN and \
		    offset[0] != 0 and offset[1] != 0:
		continue
	    list.append(neighbor)
	self.neighbors = list

	self.adjacents = []
	for offset in ((-1,0), (0,-1), (1,0), (0,1), (-1,-1), (-1,1), (1,-1),
		(1,1)):
	    x = self.x + offset[0]
	    y = self.y + offset[1]
	    adjacent = self.map.get_square(x, y)
	    if adjacent:
		self.adjacents.append(adjacent)

    # return true iff two squares are next to each other
    def is_adjacent(self, other):
	if other is None:
	    raise "other is None";
	if self is None:
	    raise "self is None";
	dx = abs(self.x - other.x)
	dy = abs(self.y - other.y)
	return (dx <= 1 and dy <= 1)

    def is_grid_aligned(self, other):
	return self.x == other.x or self.y == other.y

    def __str__(self):
	str = "%d,%d(c=" % (self.x, self.y)
	if self.character is None:
	    str = str + "None"
	else:
	    str = str + "%c" % (self.character & 0x7f)
	str = str + ",t=%d)" % self.terrain
	return str

    # lit:
    #	0 -- This square wasn't lit when we looked.
    #	1 -- This square was lit when we looked.
    def add_description(self, (char, description), lit):
	# for now, not including ' ' because it'll confuse things

	monster_chars = "@abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'&;:~]"
	item_chars = "$+=?!)*%([\\"

	if self.terrain == self.UNEXPLORED:
	    self.set_terrain(self.UNKNOWN)

	self.creature = None

	if self.BOULDER in self.items:
	    self.items.remove(self.BOULDER)

	if description == "boulder":
	    if not self.BOULDER in self.items:
		self.items.append(self.BOULDER)
	elif re.match("statue of (.*)", description):
	    pass
	elif description == "floor of a room":
	    self.set_terrain(self.FLOOR)
	elif re.match("((.* )?amulet|Amulet of Yendor)", description):
	    pass
	elif re.match("(.* )?wand", description):
	    pass
	elif re.match("neutral altar", description):
	    self.set_terrain(self.NEUTRAL_ALTAR)
	elif re.match("chaotic altar", description):
	    self.set_terrain(self.CHAOTIC_ALTAR)
	elif re.match("lawful altar", description):
	    self.set_terrain(self.LAWFUL_ALTAR)
	elif re.match("iron chain", description):
	    pass
	elif description == "open door":
	    self.set_terrain(self.OPEN_DOOR)
	elif description == "closed door":
	    self.set_terrain(self.CLOSED_DOOR)
	elif description == "broken door":
	    self.set_terrain(self.BROKEN_DOOR)
	elif re.match("(lit )?corridor", description):
	    self.set_terrain(self.CORRIDOR)
	elif description == "doorway":
	    self.set_terrain(self.DOORWAY)
	elif description == "staircase up":
	    self.set_terrain(self.STAIRCASE_UP)
	elif description == "staircase down":
	    self.set_terrain(self.STAIRCASE_DOWN)
	elif description == "wall":
	    self.set_terrain(self.WALL)
	elif description == "grave":
	    self.set_terrain(self.GRAVE)
	elif description == "tree":
	    self.set_terrain(self.TREE)
	elif description == "dark part of a room":
	    # we can do this, because we never semi-colon a ' ' part of
	    # the room we cannot see
	    if lit:
		self.set_terrain(self.WALL)
	elif description == "hole":
	    self.set_terrain(self.HOLE)
	elif description == "trap door":
	    self.set_terrain(self.TRAP_DOOR)
	elif re.match("(spiked )?pit", description):
	    self.set_terrain(self.PIT)
	elif description == "bear trap":
	    self.set_terrain(self.BEAR_TRAP)
	elif description == "magic trap":
	    self.set_terrain(self.MAGIC_TRAP)
	elif description == "arrow trap":
	    self.set_terrain(self.ARROW_TRAP)
	elif description == "rust trap":
	    self.set_terrain(self.RUST_TRAP)
	elif description == "dart trap":
	    self.set_terrain(self.DART_TRAP)
	elif description == "teleportation trap":
	    self.set_terrain(self.TELEPORT_TRAP)
	elif description == "level teleporter":
	    self.set_terrain(self.LEVEL_TELEPORTER)
	elif description == "web":
	    self.set_terrain(self.WEB)
	elif description == "sleeping gas trap":
	    self.set_terrain(self.SLEEPING_GAS_TRAP)
	elif description == "rolling boulder trap":
	    self.set_terrain(self.ROLLING_BOULDER_TRAP)
	elif description == "falling rock trap":
	    self.set_terrain(self.FALLING_ROCK_TRAP)
	elif description == "anti-magic field":
	    self.set_terrain(self.ANTI_MAGIC_FIELD)
	elif description == "squeaky board":
	    self.set_terrain(self.SQUEAKY_BOARD)
	elif char is "{":
	    self.set_terrain(self.FOUNTAIN)
	elif description == "sink":
	    self.set_terrain(self.SINK)
	elif char in monster_chars or \
		re.match("interior of (.*)", description) or \
		re.match("(.*)'s ghost", description):
	    self.creature = NetHackCreature(description)
	elif char in item_chars:
	    pass
	else:
	    raise "Unknown description: %s -- %s" % (char, description)

    def passable(self):
	return (self.terrain in self.PASSABLE_TERRAIN) and \
		(not self.BOULDER in self.items)

class NetHackMap:
    width = 80
    height = 19

    # positive y is down, positive x is to the right, to origin is at the top
    # left of the map

    def __init__(self):
	self.squares = []
	# how many times every wall that might have a secret door/passage in
	# it should be searched
	self.search_limit = 0
	self.expected = [NetHackMapSquare.STAIRCASE_DOWN,
		NetHackMapSquare.STAIRCASE_UP]
	self.changes = 0
	for y in range(self.height):
	    for x in range(self.width):
		self.squares.append(NetHackMapSquare(self, x, y))
	for square in self.squares:
	    square.update_neighbors()

    def get_square(self, x, y):
	if x < 0 or x >= self.width or y < 0 or y >= self.height:
	    return None
	index = x + self.width * y
	return self.squares[index]

    def unmark(self):
	for square in self.squares:
	    square.mark = 0
	    square.src = None

    def find_terrain(self, terrain):
	for square in self.squares:
	    if square.terrain == terrain:
		return square
	return None

    def draw(self):
	x = 0
	print "   ",
	for i in range(1,8):
	    print "        %d" % i,
	print "\n  %s" % ("0123456789" * 8)
	line = " 0"
	y = 0
	for square in self.squares:
	    if square:
		line = line + square.draw_terrain()
	    else:
		line = line + " "
	    x = x + 1
	    if x == 80:
		print line
		y = y + 1
		line = "%2d" % (y % 100)
		x = 0

    # generator
    # Generate the list of squares reachable by walking from any square in
    # dest_list in order from closest to furthest away.
    # passable_only=1: only consider squares reachable if the route to them is
    # 	currently passable (eg no boulders)
    # adjacent=1: also return all squares adjacent to the reachable ones
    def walk_from(self, dest_list, passable_only=0, adjacent=0):
	print "walk_from(%s, passable_only=%d, adjacent=%d)" % \
		(map(str, dest_list), passable_only, adjacent)
	travel_cost = {
	    (-1,-1): 1.001,
	    (-1, 0): 1,
	    (-1, 1): 1.001,
	    ( 0,-1): 1,
	    ( 0, 1): 1,
	    ( 1,-1): 1.001,
	    ( 1, 0): 1,
	    ( 1, 1): 1.001
	}
	self.unmark()
	boundary = PriorityQueue(0)
	for d in dest_list:
	    if d.passable():
		d.src = d
		boundary.put((0, d))

	while not boundary.empty() > 0:
	    (distance, square) = boundary.get()
	    #print "\tdistance=%d, square=%s" % (distance, square)

	    #print "yield", square
	    yield square

	    neighbors = []
		# also yield all adjacent squares that won't be yielded anyway
	    for vector in ((-1,0), (0,-1), (1,0), (0,1), (-1,-1), (-1,1),
		    (1,-1), (1,1)):
		neighbor = self.get_square(square.x + vector[0],
			square.y + vector[1])
		if neighbor:
		    neighbors.append(neighbor)

	    for neighbor in neighbors:
		if not neighbor in square.neighbors:
		    put = 0
		elif neighbor.src is None and \
			(passable_only is 0 or neighbor.passable()):
		    put = 1
		    dx = neighbor.x - square.x
		    dy = neighbor.y - square.y
		    if dx != 0 and dy != 0:
			# Diagonal move.
			# Check that we're not trying to squeeze between two
			# adjacent walls, which isn't going to work if we're
			# carrying to much stuff.
			if not self.get_square(neighbor.x, square.y).passable() and \
				not self.get_square(square.x, neighbor.y).passable():
			    put = 0
		else:
		    put = 0
		if put:
		    cost = neighbor.travel_cost()
		    # prefer going gridwise
		    cost = cost + travel_cost[(dx, dy)]
		    neighbor.src = square
		    if cost < 1000:
			boundary.put(((distance + cost), neighbor))
		else:
		    if adjacent:
			#print "yield adjacent", neighbor
			yield neighbor

class TodoList:
    def __init__(self):
	self.items = []

    def add(self, priority, action):
	self.items.append({'priority': priority, 'action': action})

    def remove(self, action):
	new_list = []
	for i in self.items:
	    if not i['action'] == action:
		new_list.append(i)
	self.items = new_list

    def topitem(self):
	top = self.items[0]
	for item in self.items:
	    if item['priority'] < top['priority']:
		top = item
	return top['action']

class NetHackMove:
    def __init__(self, command, args=None):
	self.command = command
	self.args = args

class TeleportException(Exception):
    pass

class NetHackPlayer:
    def __init__(self, nethack):
	self.nethack = nethack
	self.hunger = 0
	self.danger = 0
	self.except_functions = []
	# I'm not dead yet.
	self.dead = 0

    def update_map(self):
	new_map = nethack.get_map()
	map = self.map
	for y in range(0, map.height):
	    for x in range(0, map.width):
		# no point in doing a ; on the current position
		square = map.get_square(x, y)
		if square.terrain != square.UNEXPLORED and \
			self.mysquare == square:
		    continue
		index = x + y * map.width
		# skip squares that are unchanged
		if square.character == new_map[index]:
		    continue
		# skipping all empty space
		dx = abs(self.x - x)
		dy = abs(self.y - y)
		if (square.character is None and new_map[index] == ord(' ') and 
			(dx > 1 or dy > 1)):
		    continue

		square.character = new_map[index]
		while 1:
		    description = nethack.semicolon(x, y)
		    if not description is None:
			break
		square.add_description(description,
			square.is_adjacent(self.mysquare) and not self.nethack.blind)
		#print "%d,%d: %s (%d)" % (x, y, square.description,
		#	square.passable)

    " Update our danger level. 0 is completely safe. More is more dangerous. "
    def update_danger(self):
	self.danger = 0
	if self.hunger > 1:
	    self.danger = self.danger + 5 * self.hunger
	for square in self.map.squares:
	    if square.creature and square.creature.hostile:
		self.danger = self.danger + 1

    def step_next_to(self, dest):
	direction = None
	print "step from", self.mysquare, "next to", dest
	if dest is None:
	    raise Exception
	for square in self.map.walk_from(dest.adjacents, passable_only=1):
	    if square is self.mysquare:
		#print "square:", square, "square.src:", square.src, \
		#	"square.src.creature:", square.src.creature
		if not square.src.creature or square.src.creature.tame:
		    direction = (square.src.x - square.x,
			    square.src.y - square.y)
		    self.execute(NetHackMove("move", direction))
		else:
		    print "rest because %s is in %s" % (square.src.creature,
			    square.src)
		    self.execute(NetHackMove("."))

    def walk_to(self, dest):
	print "walk from", self.mysquare, "to", dest
	if dest is None:
	    raise Exception

	exc_level = self.push_except_function(lambda self: self.map.changes > 0)
	try:
	    while self.mysquare != dest:
		direction = None
		# plan a route
		for square in self.map.walk_from((dest,), passable_only=1):
		    if square is self.mysquare:
			break
		if not square is self.mysquare:
		    raise RoutePlanException(
			    "Couldn't find a route from %s to %s." % \
			    (self.mysquare, dest))

		route = []
		print "route:",
		while not square is dest:
		    route.append(square.src)
		    print square.src,
		    square = square.src
		print

		# walk the route
		for square in route:
		    if square.creature is None or square.creature.tame:
			direction = (square.x - self.mysquare.x,
				square.y - self.mysquare.y)
			self.execute(NetHackMove("move", direction))
		    else:
			print "rest because %s is in %s" % (square.creature, square)
			self.execute(NetHackMove("."))
		    # Didn't step succesfully, for whatever reason. Replan.
		    if square != self.mysquare:
			break
	except NetHackException, e:
	    if e.level < exc_level:
		raise
	self.pop_except_function()

	return not self.mysquare is dest

    def cmd_move(self, direction):
	start = self.mysquare
	target_square = self.map.get_square(self.x + direction[0],
		self.y + direction[1])
	self.nethack.move(direction)
	(self.x, self.y) = nethack.mypos()
	square = self.map.get_square(self.x, self.y)

	self.check_events()
	self.check_see_here()
	self.check_combat()
	self.check_move()
	stopped = 0
	moved = 0
	if start.terrain == start.BEAR_TRAP:
	    stopped = 1
	msg = nethack.messages
	for m in nethack.messages:
	    if m == "You stop.":
		stopped = 1
	    elif re.match("(.*) is in the way!", m):
		stopped = 1
	    elif re.match("You displaced (.*)\.", m):
		moved = 1
	    elif re.match("(.*) yelps!", m):
		pass
	    elif re.match("You pull free from (.*).", m):
		pass
	    elif re.match("You cannot escape from (.*).", m):
		stopped = 1
	    elif re.match("With great effort you move the boulder.", m):
		# the new boulder location will be picked up automatically in
		# update_map()
		# delete the old boulder
		if target_square.BOULDER in target_square.items:
		    target_square.items.remove(mysquare.BOULDER)
		moved = 1
	    elif m == 'You hear a monster behind the boulder.':
		pass
	    elif m == "Perhaps that's why you cannot move it.":
		stopped = 1
	    elif re.match("You try to move the boulder, but in vain.", m):
		# mark the other side of that boulder as wall for now. if we
		# ever get to see it it'll be fixed up
		other_side = self.map.get_square(start.x + 2 * direction[0],
			start.y + 2 * direction[1])
		if other_side:
		    other_side.terrain = other_side.WALL
		stopped = 1
	    elif re.match(
		    "However, you can squeeze yourself into a small opening.", m):
		# score!
		moved = 1
	    elif m == 'Wait!':
		stopped = 1
	    elif m == "There's something there you can't see!":
		stopped = 1
	    elif re.match("There's (.*) hiding under (.*).", m):
		stopped = 1
	    elif re.match("That's a (.*) mimic.", m):
		stopped = 1
	    elif re.match("\"(Irasshaimase|Hello|Velkommen), (.*)!  Welcome (again )?to ", m):
		moved = 1
	    elif re.match('The priest(ess)? of (.*) intones:  ' \
		    '"Pilgrim, you enter a sacred place!"', m):
		moved = 1
	    elif re.match('You experience a strange sense of peace.', m):
		moved = 1
	    elif re.match('You have a strange forbidding feeling...', m):
		moved = 1
	    elif m == 'Ouch!':
		pass
	    elif m == 'You bump into a door.':
		pass

	    ### traps ###
	    elif re.match("You fall into a pit.", m):
		square.set_terrain(square.PIT)
		moved = 1
	    elif re.match("You land on a set of sharp iron spikes.", m):
		moved = 1
	    elif re.match("You are still in a pit.", m):
		stopped = 1
	    elif re.match("You crawl to the edge of the pit.", m):
		stopped = 1
	    elif re.match("Ther(e i|')s a gaping hole under you.", m):
		square.set_terrain(square.HOLE)
		moved = 1
	    elif re.match("There is a hole here.", m):
		square.set_terrain(square.HOLE)
		moved = 1
	    elif re.match("There is a rolling boulder trap here.", m):
		square.set_terrain(square.ROLLING_BOULDER_TRAP)
		moved = 1
	    elif m == 'A trap door opens up under you!':
		self.map = NetHackMap()
		moved = 1
	    elif m == 'All the adjacent objects fall through the trap door.':
		pass
	    elif m == 'A cloud of gas puts you to sleep!':
		square.set_terrain(square.SLEEPING_GAS_TRAP)
		moved = 1
	    elif m == 'You wake up.':
		pass
	    elif re.match("An arrow shoots out at you!", m):
		square.set_terrain(square.ARROW_TRAP)
		moved = 1
	    elif re.match("A little dart shoots out at you!", m):
		square.set_terrain(square.DART_TRAP)
		moved = 1
	    elif m == 'You feel your magical energy drain away!':
		moved = 1
	    elif m == 'You feel like someone is helping you.':
		moved = 1
	    elif re.match("A bear trap closes on your foot!", m):
		square.set_terrain(square.BEAR_TRAP)
		moved = 1
	    elif re.match('A bear trap closes harmlessly over you.', m):
		square.set_terrain(square.BEAR_TRAP)
		moved = 1
	    elif re.match("You are caught in a bear trap.", m):
		stopped = 1
	    elif re.match("A tower of flame erupts from the floor.", m):
		square.set_terrain(square.FIRE_TRAP)
		moved = 1
	    elif re.match("Your (.*) smoulders.", m):
		pass
	    elif m == 'Click!':
		pass
	    elif m == 'You trigger a rolling boulder trap!':
		square.set_terrain(square.ROLLING_BOULDER_TRAP)
		moved = 1
	    elif m == 'Fortunately for you, no boulder was released.':
		pass
	    elif m == 'You step onto a level teleport trap!':
		moved = 1
	    elif m == 'You are momentarily blinded by a flash of light.':
		moved = 1
	    elif m == 'You are momentarily disoriented.':
		moved = 1
	    elif m == 'You shudder for a moment.':
		moved = 1
	    elif m == 'You suddenly yearn for your distant homeland.':
		moved = 1
	    elif m == 'You feel tired.':
		moved = 1
	    elif re.match("A trap door in the ceiling opens and a rock falls on your head", m):
		square.set_terrain(square.FALLING_ROCK_TRAP)
		moved = 1
	    elif re.match("You stumble into a spider web.", m):
		square.set_terrain(square.WEB)
		moved = 1
	    elif re.match("A board beneath you squeaks loudly.", m):
		moved = 1
	    elif re.match("Your (.*) rusts.", m):
		pass
	    elif re.match("Your (.*) is not affected.", m):
		pass
	    elif re.match("A shiver runs up and down your spine!", m):
		moved = 1
	    elif re.match("You are momentarily blinded by a flash of light!", m):
		moved = 1
	    elif re.match("You hear a deafening roar!", m):
		pass
	    elif re.match('Your pack shakes violently!', m):
		moved = 1
	    elif m == 'You are caught in a magical explosion!':
		pass
	    elif m == 'Your body absorbs some of the magical energy!':
		pass
	    elif re.match("You escape a hole.", m):
		square.set_terrain(square.HOLE)
		moved = 1
	    elif re.match("You escape a squeaky board.", m):
		square.set_terrain(square.SQUEAKY_BOARD)
		moved = 1
	    elif re.match("You escape (.*) trap.", m):
		moved = 1
	    elif re.match("Your armor is not affected.", m):
		pass
	    else:
		raise Exception, "Unexpected message: '%s'" % m

	#print "moved from", start, "to", square, "; target was", target_square
	if start == square:
	    # we didn't move
	    if stopped == 0:
		# We were stopped for some other reason than bumping into a
		# wall. The target square must be a wall.
		target_square.set_terrain(target_square.WALL)

    def check_move(self):
	others = []
	for m in nethack.messages:
	    if re.match("(\d+) gold pieces\.", m):
		pass
	    elif m == 'There are several objects here.':
		pass
	    elif re.match('"(Irasshaimase|Hello|Velkommen), \w+, welcome to Delphi."', m):
		pass
	    elif re.match('You enter an opulent throne room.', m):
		pass
	    elif re.match('You disentangle yourself.', m):
		pass
	    else:
		others.append(m)
	nethack.messages = others

    def cmd_down(self):
	self.map = NetHackMap()
	self.nethack.cmd(">")
	self.check_events()
	self.check_see_here()
	self.check_move()
	for m in nethack.messages:
	    raise Exception, "Unexpected message: '%s'" % m

    def combat(self):
	# find the closest monster and attack it
	while self.danger > 0:
	    creature = None
	    for square in self.map.walk_from((self.mysquare,), passable_only=1,
		    adjacent=1):
		if square.creature and square.creature.hostile:
		    creature = square
		    break

	    if creature:
		if creature.is_adjacent(self.mysquare):
		    print "Fight!"
		    self.execute(NetHackMove("F",
			    (square.x - self.x, square.y - self.y)))
		else:
		    print "Charge!"
		    self.step_next_to(square)
	    else:
		# No reachable monsters, nothing to see, carry on.
		self.danger = 0

    def open_door(self, door, danger_threshold=0):
	while self.danger <= danger_threshold:
	    if door.terrain == door.CLOSED_DOOR:
		self.execute(NetHackMove("o", (door.x - self.x, door.y - self.y)))
	    elif door.terrain == door.LOCKED_DOOR:
		self.execute(NetHackMove("kick", (door.x - self.x, door.y - self.y)))
	    else:
		break

    def push_except_function(self, function):
	self.except_functions.append(function)
	print "push ->", self.except_functions
	return len(self.except_functions) - 1

    def pop_except_function(self):
	return self.except_functions.pop()

    def explore(self):
	exc_level = self.push_except_function(lambda self: self.danger > 0)
	try:
	    while 1:
		if self.mysquare.terrain in (self.mysquare.UNKNOWN,
			self.mysquare.UNEXPLORED):
		    self.execute(NetHackMove(":"))

		cardinal_paths = 0
		for adjacent in self.mysquare.adjacents:
		    if self.mysquare.is_grid_aligned(adjacent) and \
			    adjacent.terrain != adjacent.WALL:
			cardinal_paths = cardinal_paths + 1
		    if adjacent.terrain in (adjacent.CLOSED_DOOR,
			    adjacent.LOCKED_DOOR):
			self.map.search_limit = 0
			self.open_door(adjacent)
		    elif adjacent.terrain is adjacent.WALL and \
			    adjacent.searched < self.map.search_limit and \
			    adjacent.is_grid_aligned(self.mysquare):
			if self.search(self.map.search_limit - adjacent.searched):
			    # found something
			    self.map.search_limit = 0

		if cardinal_paths == 1:
		    # we're at a dead end
		    print "dead end:", self.mysquare
		    if adjacent.searched < self.map.search_limit + 10:
			if self.search(self.map.search_limit + 10 - adjacent.searched):
			    # found something
			    self.map.search_limit = 0

		target = None
		for square in self.map.walk_from((self.mysquare,), passable_only=1):
		    # Visit squares if we don't know what's there.
		    if square.terrain == square.UNKNOWN:
			print "Visit %s because it's unknown" % square
			target = square
			break

		    # Visit squares if we don't know what's next to them.
		    for adjacent in square.adjacents:
			if adjacent.terrain in (adjacent.UNEXPLORED,
				    adjacent.CLOSED_DOOR, adjacent.LOCKED_DOOR):
			    print "Visit %s because it's next to unexplored/door" % square
			    target = square
			    break
			if adjacent.terrain is adjacent.WALL and \
				adjacent.searched < self.map.search_limit and \
				adjacent.is_grid_aligned(square):
			    print "Visit %s because it's next to insufficiently searched" % square
			    target = square
			    break
		    if target:
			break
		if target:
		    self.walk_to(target)
		    continue

		# find the down staircase
		staircase = None
		for square in self.map.squares:
		    if square.terrain == square.STAIRCASE_DOWN:
			staircase = square
			break
		if staircase:
		    if staircase is self.mysquare:
			self.execute(NetHackMove(">"))
		    else:
			try:
			    self.walk_to(staircase)
			except RoutePlanException:
			    self.map.search_limit = self.map.search_limit + 5
		    continue

		# search until we've found a secret door/passage
		self.map.search_limit = self.map.search_limit + 5
	except NetHackException, e:
	    print "e.level=%d, exc_level=%d" % (e.level, exc_level)
	    if e.level < exc_level:
		raise
	self.pop_except_function()
	return 1

    def cmd_open(self, vector):
	door = self.map.get_square(self.x + vector[0], self.y + vector[1])
	if self.nethack.open(vector):
	    raise "Open failed."
	self.check_events()
	self.check_booby_trap()
	for m in nethack.messages:
	    if m in ("The door opens.", 'This door is already open.'):
		door.set_terrain(door.OPEN_DOOR)
	    elif m == "This door is locked.":
		door.set_terrain(door.LOCKED_DOOR)
	    elif m == "The door resists!":
		pass
	    else:
		raise Exception, "Unexpected messages: '%s'" % m

    def cmd_fight(self, vector):
	self.nethack.fight(vector)
	self.check_events()
	self.check_combat()
	for m in nethack.messages:
	    if m == 'You attack thin air.':
		pass
	    else:
		raise Exception, "Unexpected messages: '%s'" % m

    def cmd_kick(self, vector):
	target = self.map.get_square(self.x + vector[0], self.y + vector[1])
	self.nethack.kick(vector)
	self.check_events()
	self.check_booby_trap()
	for m in nethack.messages:
	    if m == "As you kick the door, it crashes open!":
		target.set_terrain(target.BROKEN_DOOR)
	    elif m == "As you kick the door, it shatters to pieces!":
		target.set_terrain(target.DOORWAY)
	    elif m == 'You kick the door.':
		pass
	    elif m == "WHAMMM!!!":
		pass
	    elif m == '"How dare you break my door?"':
		pass
	    elif m == 'In what direction?':
		pass
	    else:
		raise Exception, "Unexpected messages: '%s'" % m

    def check_booby_trap(self):
	nethack = self.nethack

	others = []
	for m in nethack.messages:
	    if re.match('KABOOM', m):
		pass
	    elif re.match('The door was booby-trapped.', m):
		pass
	    elif re.match('You stagger...', m):
		pass
	    else:
		others.append(m)

	nethack.messages = others

    def check_events(self):
	nethack = self.nethack

	others = []
	for m in nethack.messages:
	    if re.match("(.*) picks up (.*).", m):
		pass
	    elif re.match("(.*) moves only reluctantly.", m):
		pass
	    elif re.match("(.*) puts on (.*).", m):
		pass
	    elif re.match("(.*) (throws|shoots) (.*).", m):
		pass
	    elif re.match("(.*) misfires.", m):
		pass
	    elif m == 'It is missed.':
		pass
	    elif re.match('(.*) is hit.', m):
		pass
	    elif re.match('(.*) is blinded by the cream pie.', m):
		pass
	    elif re.match("(.*) hurls (.*) potion.", m):
		pass
	    elif re.match("(.*) drinks (.*) potion.", m):
		pass
	    elif re.match("(.*) looks (much )?better.", m):
		pass
	    elif re.match("(.*) looks completely healed.", m):
		pass
	    elif re.match("(.*) suddenly mutates.", m):
		pass
	    elif re.match("(.*) (\w+) falls around (\w+).", m):
		pass
	    elif re.match("(.*) (\w+) can no longer hold \w+ .*.", m):
		pass
	    elif re.match("(.*) \w+ falls to the floor.", m):
		pass
	    elif re.match("(.*) engulfs you.", m):
		pass
	    elif m == "You can't see in here!":
		pass
	    elif m == 'You are pummeled with debris!':
		pass
	    elif m == 'You are laden with moisture and can barely breathe!':
		pass
	    elif m == "You get expelled!":
		pass
	    elif re.match("(.*) explodes.", m):
		pass
	    elif re.match("(.*) is caught in (.*) explosion.", m):
		pass
	    elif re.match("You are blinded by a blast of light.", m):
		pass
	    elif re.match("(.*) seems more experienced.", m):
		pass
	    elif re.match("The (flask|flagon|jar|bottle|vial|carafe|phial) crashes on your head and breaks into shards.", m):
		pass
	    elif m == 'You feel rather tired.':
		pass
	    elif re.match("It suddenly gets dark.", m):
		pass
	    elif re.match("You feel somewhat dizzy.", m):
		pass
	    elif re.match("(.*) staggers.", m):
		pass
	    elif re.match("(.*) seems disoriented.", m):
		pass
	    elif re.match("(.*) changes into (.*).", m):
		pass
	    elif re.match("(.*) summons help.", m):
		pass
	    elif re.match("You feel hemmed in.", m):
		pass
	    elif re.match("(.*) shrieks.", m):
		pass
	    elif re.match("(.*) stings.", m):
		pass
	    elif re.match("(.*) zaps (.*).", m):
		pass
	    elif re.match("(.*) has made a hole in the floor.", m):
		pass
	    elif re.match("(.*) falls through...", m):
		pass
	    elif re.match("(.*) dives through...", m):
		pass
	    elif re.match("(.*) whizzes by you.", m):
		pass
	    elif re.match("(.*) casts a spell.", m):
		pass
	    elif re.match("You reel...", m):
		pass
	    elif re.match("Your head suddenly aches painfully.", m):
		pass
	    elif re.match('Your brain is on fire.', m):
		pass
	    elif re.match("(.*) points (all around|at you), then curses.", m):
		pass
	    elif m == 'You hear a mumbled curse.':
		pass
	    elif re.match("(.*) shatters.", m):
		pass
	    elif re.match("Suddenly you cannot see (.*).", m):
		pass
	    elif re.match("(.*) is suddenly moving faster.", m):
		pass
	    elif re.match("(.*) bounces.", m):
		pass
	    elif re.match("(.*) reads (.*).", m):
		pass
	    elif m == "You hear a nearby zap.":
		pass
	    elif re.match("Suddenly, you notice (.*).", m):
		pass
	    elif re.match("(.*) was hidden under (.*)!", m):
		pass
	    elif re.match("(.*) turns to flee.", m):
		pass
	    elif re.match("(.*) tries to (snatch|grab) your (.*) but gives up.", m):
		pass
	    elif re.match("You are hit by (.*).", m):
		pass
	    elif re.match("You are almost hit by (.*).", m):
		pass
	    elif re.match("(.*) is almost hit by (.*).", m):
		pass
	    elif re.match("(.*) is hit by (.*).", m):
		pass
	    elif re.match("(.*) is not affected.", m):
		pass
	    elif re.match("(.*) charms you\.", m):
		pass
	    elif re.match("You gladly start removing your armor.", m):
		pass
	    elif re.match("You gladly hand over your (.*).", m):
		pass
	    elif re.match('(.*) tries to rob you, but there is nothing to steal!',
		    m):
		pass
	    elif re.match("(.*) stole (.*)\.", m):
		pass
	    elif re.match("(.*) tries to run away with your (.*)\.", m):
		pass
	    elif re.match("(.*) steals (.*).", m):
		pass
	    elif re.match("(.*) pretends to be friendly\.", m):
		pass
	    elif re.match("(.*) was poisoned.", m):
		pass
	    elif re.match("You feel weaker.", m):
		pass
	    elif re.match("The poison doesn't seem to affect you.", m):
		pass
	    elif re.match("(.*) ((just )?misses|hits|bites|kicks|butts).", m):
		pass
	    elif re.match("(.*) breathes frost.", m):
		pass
	    elif re.match("You don't feel cold.", m):
		pass
	    elif re.match("(.*) grabs you.", m):
		pass
	    elif re.match("You are being choked.", m):
		pass
	    elif re.match('You are put to sleep by (.*).', m):
		pass
	    elif re.match('The combat suddenly awakens you.', m):
		pass
	    elif re.match('You wake up.', m):
		pass
	    elif re.match('(.*) blinds you.', m):
		pass
	    elif re.match("(.*) yelps!", m):
		pass
	    elif re.match('You feel (feverish|very sick).', m):
		pass
	    elif re.match('You turn into a .*.', m):
		pass
	    elif re.match('You return to human form.', m):
		pass
	    elif re.match('You can no longer hold your \w+.', m):
		pass
	    elif re.match('You find you must drop your weapon.', m):
		pass
	    elif m == 'Use the command #monster to summon help.':
		pass
	    elif m == 'Your movements are slowed slightly because of your load.':
		pass
	    elif m == 'Your purse feels lighter.':
		pass
	    elif re.match("You get zapped!", m):
		pass
	    elif re.match(".* gets zapped!", m):
		pass
	    elif m == 'You feel something move nearby.':
		pass
	    elif m == 'You feel a bit steadier now.':
		pass
	    elif re.match("(.*) touches (.*)!", m):
		pass
	    elif re.match("(.*) (bites|misses) (.*)\.", m):
		pass
	    elif re.match("(.*) eats (.*)\.", m):
		pass
	    elif re.match("(.*) wields (.*)!", m):
		pass
	    elif re.match("(.*) tries to wield (.*).", m):
		pass
	    elif re.match("(.*) is welded to (his|her) hand.", m):
		pass
	    elif re.match("(.*) (thrusts|swings) (.*).", m):
		pass
	    elif re.match("(.*) casts aspersions on your ancestry.", m):
		pass
	    elif m == '"Why search for the Amulet?  ' \
		    'Thou wouldst but lose it, cretin."':
		pass
	    elif m == '"Verily, thy corpse could not smell worse!"':
		pass
	    elif m == '"Run away!  Live to flee another day!"':
		pass
	    elif m == '"Look!  Thy bootlace is undone!"':
		pass
	    elif m == '"Thinkest thou it shall tickle as I rip out thy lungs?"':
		pass
	    elif m == '"Methinks thou wert unnaturally stirred by yon corpse back there, eh, varlet?"':
		pass
	    elif m == '"Mercy!  Dost thou wish me to die of laughter?"':
		pass
	    elif m == '"Doth pain excite thee?  Wouldst thou prefer the whip?"':
		pass
	    elif m == '"I\'ve met smarter (and prettier) acid blobs."':
		pass
	    elif re.match("(.*) welds itself to (.*).", m):
		pass
	    elif re.match("(.*) suddenly falls asleep.", m):
		pass
	    elif m in ("You hear water falling on coins.",
		    "You hear bubbling water."):
		if not NetHackMapSquare.FOUNTAIN in self.map.expected:
		    self.map.expected.append(NetHackMapSquare.FOUNTAIN)
	    elif m in ("You hear a slow drip.", "You hear a gurgling noise."):
		if not NetHackMapSquare.SINK in self.map.expected:
		    self.map.expected.append(NetHackMapSquare.SINK)
	    elif m == "You hear someone counting money.":
		pass
	    elif m == "You hear a crunching sound.":
		pass
	    elif m == "You hear a clank.":
		pass
	    elif m == "You hear the footsteps of a guard on patrol.":
		pass
	    elif m == "You hear the splashing of a naiad.":
		pass
	    elif m == "You hear a jackal howling at the moon.":
		pass
	    elif m == "You hear the roaring of an angry bear!":
		pass
	    elif m == 'You hear something crash through the floor.':
		pass
	    elif m == "You hear a door open.":
		pass
	    elif m == "You hear crashing rock.":
		pass
	    elif m == "You feel an unexpected draft.":
		pass
	    elif m == "The dungeon acoustics noticeably change.":
		pass
	    elif m == "Suddenly, a section of wall closes up!":
		pass
	    elif m == "You hear a chugging sound.":
		pass
	    elif re.match("For some reason, .* presence is known to you.", m):
		pass
	    elif re.match('You feel aggravated at .*.', m):
		pass
	    elif m == "You feel less confused now.":
		pass
	    elif m == "You smell charred flesh.":
		pass
	    elif m == "You hear a distant squeak.":
		pass
	    elif m == 'You hear a strange wind.':
		pass
	    elif m == 'You hear convulsive ravings.':
		pass
	    elif m == 'You hear snoring snakes.':
		pass
	    elif m == "Kaablamm!":
		pass
	    elif m == "You hear an explosion in the distance!":
		pass
	    elif re.match("A board beneath (.*) squeaks loudly.", m):
		pass
	    elif m == "You hear a blast.":
		pass
	    elif m == 'You hear rumbling in the distance.':
		pass
	    elif m == "You hear distant howling.":
		pass
	    elif m == "You hear someone cursing shoplifters.":
		pass
	    elif m == "You hear the chime of a cash register.":
		pass
	    elif m == "You see a door open.":
		pass
	    elif m == 'The yellow light flows under the door.':
		pass
	    elif m == 'You see a cave spider hatch.':
		pass
	    elif re.match('(.*) suddenly disappears.', m):
		pass
	    elif re.match('Suddenly, (.*) disappears out of sight.', m):
		pass
	    elif m in ("You are beginning to feel hungry.",
		    'You are getting the munchies.'):
		self.hunger = 1
	    elif m == "You are beginning to feel weak.":
		self.hunger = 2
	    elif m == "You faint from lack of food.":
		self.hunger = 3
	    elif m == "You regain consciousness.":
		pass
	    elif m == "You can see again.":
		pass
	    elif re.match("(.*) needs food, badly!", m):
		self.hunger = 2
	    elif re.match("(.*), your life force is running out.", m):
		pass
	    elif re.match("You hear some noises( in the distance)?.", m):
		pass
	    elif re.match("You have a sad feeling for a moment, then it passes.", m):
		pass
	    elif re.match('You feel sad for a moment.', m):
		pass
	    elif re.match('You feel worried about your (.*).', m):
		pass
	    elif re.match("(.*) falls into a pit!", m):
		pass
	    elif re.match("(.*) is (killed|destroyed)!", m):
		pass
	    elif re.match("(.*) suddenly drops from the ceiling.", m):
		pass
	    elif re.match("(.*) drops (.*)\.", m):
		pass
	    elif m == "You feel tough!":
		pass
	    elif m == "You feel quick!":
		pass
	    elif m == "You must be leading a healthy life-style.":
		pass
	    elif m == "You feel charismatic!":
		pass
	    elif m == "You feel strong!":
		pass
	    elif m == "You must have been exercising.":
		pass
	    elif m == "You feel healthy!":
		pass
	    elif m == "You feel stealthy!":
		pass
	    elif m == 'You feel awake!':
		pass
	    elif m == "You feel wise!":
		pass
	    elif m == 'You must have been very observant.':
		pass
	    elif m == "You feel foolish!":
		pass
	    elif m == "You feel agile!":
		pass
	    elif m == "You must have been working on your reflexes.":
		pass
	    elif m == "Suddenly one of the Vault's guards enters!":
		pass
	    elif m == "Suddenly, the guard disappears.":
		pass
	    elif m == re.match("(.*) is about to die.", m):
		pass
	    elif re.match("You die", m):
		pass
	    elif re.match('A watchman yells:', m):
		pass
	    elif m == '"Halt, thief!  You\'re under arrest!"':
		pass
	    elif m == 'You see an angry guard approaching!':
		pass
	    elif m == 'Click!':
		pass
	    elif re.match("(.*) triggers something.", m):
		pass
	    elif re.match("(.*) triggers a rolling boulder trap.", m):
		pass
	    elif re.match('(.*) concentrates.', m):
		pass
	    elif re.match('A wave of psychic energy pours over you.', m):
		pass
	    elif m == 'You sense a faint wave of psychic energy.':
		pass
	    elif re.match("(.*)'s tentacles suck you.", m):
		pass
	    elif re.match('Your brain is eaten.', m):
		pass
	    elif re.match('You feel (very )?stupid.', m):
		pass
	    elif re.match("(.*) escapes (.*)stairs.", m):
		pass
	    elif re.match(".* oozes under the door.", m):
		pass
	    elif re.match("You can move again.", m):
		pass
	    elif re.match('Everything looks SO boring now.', m):
		pass
	    elif re.match('(.*) is caught in a spider web.', m):
		pass
	    else:
		others.append(m)

	if re.match("Do you want (your possessions identified|to see what you had when you died|to see your attributes)", self.nethack.prompt):
	    self.dead = 1
	    self.check_exceptions()
	elif re.match("Call a (.*) potion:", self.nethack.prompt):
	    self.nethack.cmd("used\n")
	elif len(self.nethack.prompt):
	    raise "Unexpected prompt: %s" % self.nethack.prompt

	nethack.messages = others

    def check_see_here(self):
	nethack = self.nethack

	others = []
	square = self.map.get_square(self.x, self.y)
	here_list = None
	for m in nethack.messages:
	    if not here_list is None:
		here_list.append(m)
	    elif re.match("You (see|feel) here (.*)\.", m):
		pass
	    elif re.match("Things that (you feel|are) here:", m):
		here_list = []
	    elif re.match(
		    "You try to feel what is lying here on the (floor|ground|fountain).", m):
		pass
	    elif m == "There is a doorway here.":
		square.set_terrain(square.DOORWAY)
	    elif m == "There is a fountain here.":
		square.set_terrain(square.FOUNTAIN)
	    elif m == "There is a broken door here.":
		square.set_terrain(square.BROKEN_DOOR)
	    elif m == "There is an open door here.":
		square.set_terrain(square.OPEN_DOOR)
	    elif m == 'Something is engraved here on the headstone.':
		pass
	    elif m == 'There is a grave here.':
		square.set_terrain(square.GRAVE)
	    elif m == "There is a staircase up here.":
		square.set_terrain(square.STAIRCASE_UP)
	    elif m == "There is a staircase down here.":
		square.set_terrain(square.STAIRCASE_DOWN)
	    elif m == "There is an arrow trap here.":
		square.set_terrain(square.ARROW_TRAP)
	    elif m == "There is a dart trap here.":
		square.set_terrain(square.DART_TRAP)
	    elif m == "There is a web here.":
		square.set_terrain(square.WEB)
	    elif m == "There is a teleportation trap here.":
		square.set_terrain(square.TELEPORT_TRAP)
	    elif m == "There is an anti-magic field here.":
		square.set_terrain(square.ANTI_MAGIC_FIELD)
	    elif m == "There is a sink here.":
		square.set_terrain(square.SINK)
	    elif m == "There is a grave here.":
		square.set_terrain(square.GRAVE)
	    elif re.match("There is a (spiked )?pit here.", m):
		square.set_terrain(square.PIT)
	    elif m == "There is a falling rock trap here.":
		square.set_terrain(square.FALLING_ROCK_TRAP)
	    elif m == "There is a bear trap here.":
		square.set_terrain(square.BEAR_TRAP)
	    elif m == "There is a magic trap here.":
		square.set_terrain(square.MAGIC_TRAP)
	    elif m == "There is a rust trap here.":
		square.set_terrain(square.RUST_TRAP)
	    elif m == "There is a squeaky board here.":
		square.set_terrain(square.SQUEAKY_BOARD)
	    elif re.match("There is an altar to (.*) \(neutral\) here.", m):
		square.set_terrain(square.NEUTRAL_ALTAR)
	    elif re.match("There is an altar to (.*) \(chaotic\) here.", m):
		square.set_terrain(square.CHAOTIC_ALTAR)
	    elif re.match("There is an altar to (.*) \(lawful\) here.", m):
		square.set_terrain(square.LAWFUL_ALTAR)
	    elif re.match("There is an opulent throne here.", m):
		pass
	    elif m == "There's some graffiti on the floor here.":
		pass
	    elif m == "Something is written here in the dust.":
		pass
	    elif m == 'There are many objects here.':
		pass
	    elif re.match("You read: (.*)", m):
		pass
	    else:
		others.append(m)

	nethack.messages = others

    def check_combat(self):
	nethack = self.nethack

	others = []
	for m in nethack.messages:
	    if re.match("You (kill|destroy) (.*)!", m):
		pass
	    elif re.match("You are caught in the (.*)'s explosion.", m):
		pass
	    elif re.match("You (hit|smite|bite) (.*).", m):
		pass
	    elif re.match("(.*) growls.", m):
		pass
	    elif re.match("You miss (.*).", m):
		pass
	    elif re.match("You are frozen by (.*).", m):
		pass
	    elif re.match("You stagger...", m):
		pass
	    elif re.match("You are splashed by (.*).", m):
		pass
	    elif re.match("Your (.*) corrodes.", m):
		pass
	    elif re.match("The air crackles around (.*).", m):
		pass
	    elif m == 'You feel a mild chill.':
		pass
	    elif m == 'You feel mildly chilly.':
		pass
	    elif m == 'You are suddenly very hot!':
		pass
	    elif m == "You're on fire!":
		pass
	    elif re.match('(.*) divides as you hit it!', m):
		pass
	    elif m == 'You hear the rumble of distant thunder...':
		pass
	    elif re.match('You feel more confident in your (.*) skills.', m):
		pass
	    elif re.match("Welcome to experience level (\d+).", m):
		pass
	    elif re.match("You begin (bashing|striking) monsters with your (gloved|bare) hands.", m):
		pass
	    elif re.match('(.*) gets angry!', m):
		pass
	    else:
		others.append(m)

	nethack.messages = others

    # take it from the DYWYPI prompt on
    def finish(self):
	if re.match("Do you want (your possessions identified|to see what you had when you died)",
		self.nethack.prompt):
	    self.nethack.cmd("y")
	    inventory = self.nethack.messages
	    print "Inventory:", inventory

	if re.match("Do you want to see your attributes", self.nethack.prompt):
	    self.nethack.cmd("y")
	    attributes = self.nethack.messages
	    print "Attributes:", attributes

	if re.match("Do you want an account of creatures vanquished",
		self.nethack.prompt):
	    self.nethack.cmd("y")
	    creatures = self.nethack.messages
	    print "Creatures:", creatures

	if re.match("Do you want to see your conduct", self.nethack.prompt):
	    self.nethack.cmd("y")
	    conduct = self.nethack.messages
	    print "Conduct:", conduct
	    p = 0
	    for c in conduct:
		if re.match("(Farvel|Good)", c):
		    p = 1
		elif p:
		    log.append(c)

	for m in self.nethack.messages:
	    if re.match("No  Points", m):
		break
	    print m

    def play(self):
	nethack = self.nethack
	self.map = NetHackMap()

	try:
	    nethack.cmd("", 1000)

	    if re.match("Shall I", nethack.prompt):
		log.append("New game.")
		# go through the new character creation song and dance
		nethack.cmd("y")
		while 1:
		    if len(nethack.messages) > 0:
			m = nethack.messages[0]
			nethack.messages = nethack.messages[1:]
			if re.match(".*, welcome to NetHack!", m):
			    break
		    else:
			nethack.cmd("")
	    elif nethack.prompt == "":
		log.append("Resume saved game.")
		while 1:
		    if len(nethack.messages) > 0:
			m = nethack.messages[0]
			nethack.messages = nethack.messages[1:]
			if re.match(".*, welcome back to NetHack!", m):
			    break
		    else:
			nethack.cmd("")
	    else:
		raise Exception, "Unexpected prompt: '%s'" % nethack.prompt

	    # start playing the game

	    # deal with messages that occur only on startup
	    self.check_events()
	    (self.x, self.y) = nethack.mypos()
	    self.check_see_here()
	    for m in nethack.messages:
		match = re.match("You are an? (\w+) (male|female)? ?(\w+) (\w+).", \
			m)
		if match:
		    self.alignment = match.group(1)
		    self.gender = match.group(2)
		    self.race = match.group(3)
		    self.occupation = match.group(4)

		    print "alignment=%s, gender=%s, race=%s, class=%s" % (
			    self.alignment, self.gender, self.race,
			    self.occupation)
		elif m == "You are lucky!":
		    pass
		elif m == "Be careful!":
		    pass
		elif re.match("(Full|New) moon tonight.", m):
		    pass
		elif re.match("(\d+) gold pieces\.", m):
		    pass
		else:
		    raise Exception, "Unknown message: '%s'" % m
	    nethack.messages = []
	    self.update()

	    exc_level = self.push_except_function(lambda self: self.dead)
	    try:
		while 1:
		    if self.hunger > 1:
			self.execute(NetHackMove("pray"))
		    elif self.danger > 0:
			self.combat()
		    else:
			self.explore()
	    except NetHackException, e:
		if e.level < exc_level:
		    raise
	    self.pop_except_function()
	    self.finish()

	except:
	    print "terminal:"
	    self.nethack.terminal.draw()
	    print "map:"
	    self.map.draw()
	    print "prompt: '%s'" % self.nethack.prompt
	    print "messages:", self.nethack.messages
	    log.append("crashed: %s" % sys.exc_info()[0])
	    raise

    def update(self):
	(self.x, self.y) = nethack.mypos()
	self.depth = nethack.mydepth()
	self.mysquare = self.map.get_square(self.x, self.y)
	self.update_map()
	if self.mysquare is None:
	    raise Exception, "I'm outside the map @%d,%d" % (self.x,
		    self.y)
	self.mysquare.visited = self.mysquare.visited + 1
	self.update_danger()

    def execute(self, move):
	jump_table = {
	    ":":    self.cmd_colon,
	    "s":    self.cmd_search,
	    "move": self.cmd_move,
	    "o":    self.cmd_open,
	    "kick": self.cmd_kick,
	    ">":    self.cmd_down,
	    ".":    self.cmd_rest,
	    "F":    self.cmd_fight,
	    "pray": self.cmd_pray,
	}
	self.map.changes = 0
	(old_x, old_y) = (self.x, self.y)
	old_depth = self.depth
	function = jump_table[move.command]
	if move.args:
	    function(move.args)
	else:
	    function()

	# do the standard checks
	self.update()
	self.check_exceptions()

    def check_exceptions(self):
	for level in range(len(self.except_functions)):
	    function = self.except_functions[level]
	    if function(self):
		print "raise level %d/%d exception" % \
			(level, len(self.except_functions) - 1)
		traceback.print_stack()
		raise NetHackException(self, level)

    def cmd_pray(self):
	nethack.pray()
	self.check_events()
	for m in nethack.messages:
	    if re.match("You begin praying to (.*).", m):
		pass
	    elif re.match("You finish your prayer.", m):
		pass
	    elif re.match("You feel that (.*) is (.*).", m):
		pass
	    elif re.match("The voice of (.*) (booms( out)?|thunders|rings out):", m):
		pass
	    elif re.match("A black glow surrounds you.", m):
		pass
	    elif re.match("(.*) starts to attack you, but pulls back.", m):
		pass
	    elif m == 'Suddenly, a bolt of lightning strikes you!':
		pass
	    elif m == 'You fry to a crisp.':
		pass
	    elif re.match('The couatl of (.*) brushes against your leg.', m):
		pass
	    elif m == 'You are surrounded by a shimmering light.':
		pass
	    elif m == 'Your stomach feels content.':
		self.hunger = 0
	    elif m == 'You feel much better.':
		pass
	    elif m == 'You feel purified.':
		pass
	    elif m == '"Thou art arrogant, mortal."  "Thou must relearn thy lessons!"':
		pass
	    elif m == '"Thou must relearn thy lessons!"  You feel foolish!':
		pass
	    elif m == '"Thou durst call upon me?"  "Then die, mortal!"':
		pass
	    elif m == '"Then die, mortal!"':
		pass
	    elif m == '"Thou hast strayed from the path, mortal."':
		pass
	    elif m == 'You are being punished for your misbehavior!':
		pass
	    elif re.match('(Farvel|Goodbye) level (\d+).', m):
		pass
	    elif re.match('You feel slow.', m):
		pass
	    else:
		raise Exception, "Unexpected message: '%s'" % m

    def cmd_colon(self):
	nethack.cmd(":")
	self.check_see_here()
	self.check_events()
	square = self.mysquare
	if square.terrain in (square.UNKNOWN, square.UNEXPLORED):
	    square.set_terrain(square.UNKNOWN_PASSABLE)
	for m in nethack.messages:
	    if re.match("You (see|feel) no objects here.", m):
		pass
	    elif re.match("You look around to see what is lying in (.*).", m):
		pass
	    elif re.match("There is a rolling boulder trap here.", m):
		square.set_terrain(square.ROLLING_BOULDER_TRAP)
	    else:
		raise Exception, "Unexpected messages: '%s'" % m

    def search(self, count):
	original_wall_count = 0
	for adjacent in self.mysquare.adjacents:
	    if adjacent.terrain == adjacent.WALL:
		original_wall_count = original_wall_count + 1
	for i in range(count):
	    self.execute(NetHackMove("s"))
	    if self.map.changes:
		return 1
	return 0

    def cmd_search(self):
	self.nethack.cmd("s")
	for adjacent in self.mysquare.adjacents:
	    adjacent.searched = adjacent.searched + 1
	self.mysquare.searched = self.mysquare.searched + 1
	self.check_events()
	for m in nethack.messages:
	    if re.match('You find (.*) trap.', m):
		pass
	    elif re.match('You find (.*).', m):
		pass
	    elif m == 'You feel an unseen monster!':
		pass
	    else:
		raise Exception, "Unexpected messages: '%s'" % m

    def cmd_rest(self):
	self.nethack.cmd(".")
	self.check_events()
	for m in nethack.messages:
	    raise Exception, "Unexpected messages: '%s'" % m

class Log:
    def __init__(self, filename):
	self.filename = filename
	self.fd = open(filename, "a")

    def append(self, string):
	self.fd.write("%s: %s\n" % (time.ctime(), string))

    def __del__(self):
	self.fd.close()

nethack_output = "/tmp/nethack_fifo"
if not os.path.exists(nethack_output):
    os.mkfifo(nethack_output)
#os.system("xterm -rv -e ttyrec -e 'cat %s' &" % nethack_output)
os.system("xterm -rv -e 'cat %s' &" % nethack_output)
nethack_output_fd = open(nethack_output, 'w')

log = Log("bottie.log")
nethack = NetHack(nethack_output_fd);
player = NetHackPlayer(nethack)
player.play()
