import datetime
import json
import os
import re
import signal
import sys
import threading
import queue

import psutil

from anoptions import Parameter, Options

# pylint: disable=useless-object-inheritance
# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-return-statements


class ProcessEvent(object):
  def __init__(self, headers, payload, dt):
    self.headers  = headers
    self.payload  = payload
    self.dt       = dt
    self.process  = payload["processname"] if "processname" in payload else None

    rex = re.match('^PROCESS_STATE_(.+)$', headers["eventname"])
    if rex:
      self.event = rex.groups()[0]
      if "expected" in payload:
        self.expected = (payload["expected"] == "1")
      else:
        self.expected = None
    else:
      self.event    = None
      self.expected = None


  def get(self, for_json=False):
    return {
      "dt": self.dt if for_json is False else self.dt.isoformat(),
      "process":  self.process,
      "event":    self.event,
      "expected": self.expected
    }


  def getall(self, for_json=False):
    return {
      "dt": self.dt if for_json is False else self.dt.isoformat(),
      "headers": self.headers,
      "payload": self.payload
    }


class Watchdog(object):
  def __init__(self, config):
    self.config = config

    self.exit_signals = [
      signal.SIGHUP,
      signal.SIGINT,
      signal.SIGTERM
    ]

    self.stop_signals = [
      signal.SIGQUIT
    ]

    self.supervisor_pid = self.get_supervisor_pid()
    self.supervisor_proc = self.get_process(self.supervisor_pid)

    self.eventq = queue.Queue()
    self.signalq = queue.Queue()

    self.listener_thread = threading.Thread(target=self.listener)
    self.listener_thread.name = 'listener'
    self.listener_thread.setDaemon(True)
    self.listener_thread.start()

    self.eh_thread = threading.Thread(target=self.eventhandler)
    self.eh_thread.name = 'eventhandler'
    self.eh_thread.setDaemon(True)
    self.eh_thread.start()


  @staticmethod
  def write_stdout(s):
    # only eventlistener protocol messages may be sent to stdout
    sys.stdout.write(s)
    sys.stdout.flush()


  @staticmethod
  def write_stderr(s):
    # write other messages here
    sys.stderr.write(s)
    sys.stderr.flush()


  @classmethod
  def write_json_stderr(cls, msg_template, json_data):
    msg = msg_template.format(data=json.dumps(json_data, indent=4))
    cls.write_stderr(msg + "\n")


  def listener(self):
    while True:
      # transition from ACKNOWLEDGED to READY
      self.write_stdout('READY\n')

      # read header line
      line = sys.stdin.readline()
      headers = dict([ x.split(':') for x in line.split() ])

      # read payload line
      text = sys.stdin.read(int(headers['len']))
      payload = dict([ x.split(':') for x in text.split() ])

      pevent = ProcessEvent(headers, payload, datetime.datetime.now())

      self.eventq.put_nowait(pevent)

      if self.config["silent"] is False:
        self.write_json_stderr("{data}", pevent.getall(for_json=True))

      # transition from READY to ACKNOWLEDGED
      self.write_stdout('RESULT 2\nOK')


  @staticmethod
  def file_exists(filename):
    return filename is not None and os.path.exists(filename) and os.path.isfile(filename)


  @staticmethod
  def load_text(filename):
    result = None
    if filename == '-':
      f = sys.stdin
    else:
      f = open(filename, 'r', encoding='utf8')
    with f as file:
      result = file.read()
    return result


  def get_supervisor_pid(self):
    pidfile = self.config["pidfile"]
    if not self.file_exists(pidfile):
      self.write_stderr("Pidfile not found -- exiting\n")
      sys.exit(1)
    pid = int(self.load_text(pidfile).strip())
    return pid


  @staticmethod
  def get_process(pid):
    # Will return psutil.Process for pid, and None if pid is not found
    try:
      p = psutil.Process(pid)
      return p
    except psutil.NoSuchProcess:
      return None


  def make_supervisor_exit(self):
    self.supervisor_proc.send_signal(signal.SIGQUIT)


  def eventhandler(self):
    last_tick = datetime.datetime.now()
    status = {}

    def statefilter(processevent, config, dt):
      # These are known good states, no need to process
      if processevent.event in ('STARTING', 'RUNNING'):
        return False

      # Inspect if stopped / stopping processed have extended their stop time allowance
      if processevent.event in ('STOPPED', 'STOPPING'):
        t = config["wait_stopped"]
        if t != 0:
          limit_dt = processevent.dt + datetime.timedelta(seconds=t)
          if limit_dt < dt:
            return True
        return False

      # Inspect if expectedly exited processed have extended their stop time allowance
      if processevent.event == 'EXITED' and processevent.expected is True:
        t = config["wait_expected"]
        if t != 0:
          limit_dt = processevent.dt + datetime.timedelta(seconds=t)
          if limit_dt >= dt:
            return False
        return True

      # Inspect other (unexpected) exits ('BACKOFF', 'EXITED', 'FATAL', 'UNKNOWN')
      t = config["wait_unexpected"]
      if t != 0:
        limit_dt = processevent.dt + datetime.timedelta(seconds=t)
        if limit_dt >= dt:
          return False
      return True

    while True:
      try:
        # Check tick
        if (
          self.config["notick"] is False and
          last_tick + datetime.timedelta(seconds=75) < datetime.datetime.now()
        ):
          self.write_stderr("Did not receive TICK_60 in time -- exiting\n")
          # Use SIGUSR1 internally to signal our main thread to start exiting
          self.signalq.put(signal.SIGUSR1)

        # Process current states
        dt = datetime.datetime.now()
        lst = list(filter(
          lambda x: statefilter(x, self.config, dt),
          status.values()
        ))
        if len(lst) > 0:
          msg = "Process states that are violating required state:\n{data}\nExiting.\n"
          _lst = [ x.get(for_json=True) for x in lst ]
          self.write_json_stderr(msg, _lst)
          # Use SIGUSR1 internally to signal our main thread to start exiting
          self.signalq.put(signal.SIGUSR1)

        # Wait for new event
        event = self.eventq.get(timeout=1)

        # We got an event
        if event.event is not None:
          status[event.process] = event
        elif event.headers["eventname"] == "TICK_60":
          last_tick = datetime.datetime.now()

      except queue.Empty:
        pass


  def signal_handler(self, signum, frame):
    sig = signal.Signals(signum) # pylint: disable=no-member
    self.signalq.put_nowait(sig)


  def run(self):
    class StartExit(Exception):
      pass

    for sig in [ *self.exit_signals, *self.stop_signals ]:
      signal.signal(sig, self.signal_handler)

    try:
      while True:
        # Check that all out daemon threads are alive
        for t in [ self.listener_thread, self.eh_thread ]:
          if t.is_alive() is False:
            self.write_stderr("Thread {} has exited -- exiting".format(t.name))
            raise StartExit
        try:
          sig = self.signalq.get(timeout=1)
          if sig in self.exit_signals or sig == signal.SIGUSR1:
            # We will exit with nonzero exitcode
            # and kill the supervisor with us
            raise StartExit
          if sig in self.stop_signals:
            # We will exit with an exitcode of zero
            # and leave supervisord running
            break
        except queue.Empty:
          pass
    except (KeyboardInterrupt, StartExit):
      self.make_supervisor_exit()
      sys.exit(1)


def main(argv):
  parameters = [
    Parameter("pidfile", str, "pidfile"),
    Parameter("waitu",   int, "wait_unexpected", short_name='w', default=0),
    Parameter("waite",   int, "wait_expected",   short_name='W', default=0),
    Parameter("waits",   int, "wait_stopped",    short_name='S', default=0),
    Parameter("notick", Parameter.flag, "notick"),
    Parameter("silent", Parameter.flag, "silent")
  ]

  opt = Options(parameters, argv, "watchdog")
  config = opt.eval()

  required = [ "pidfile" ]
  for x in required:
    if x not in config:
      Watchdog.write_stderr("{} is a required parameter -- exiting\n".format(x.upper()))
      sys.exit(1)

  o = Watchdog(config)
  o.run()


if __name__ == '__main__':
  main(sys.argv[1:])
