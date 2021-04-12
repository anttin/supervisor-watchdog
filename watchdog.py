import anoptions
import datetime
import json
import psutil
import re
import signal
import sys
import threading
import queue

from anoptions import Parameter, Options
from collections import namedtuple

ProcessEvent = namedtuple('ProcessEvent', 'dt process event expected')


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

      data = {
        "headers": headers,
        "payload": payload,
        "dt": datetime.datetime.now()
      }

      self.eventq.put_nowait(data)

      if self.config["silent"] is False:
        data["dt"] = data["dt"].isoformat()
        self.write_stderr(json.dumps(data, indent=4)+"\n")
      
      # transition from READY to ACKNOWLEDGED
      self.write_stdout('RESULT 2\nOK')


  @staticmethod
  def file_exists(filename):
    import os
    return filename is not None and os.path.exists(filename) and os.path.isfile(filename)  


  @staticmethod
  def load_text(filename):
    result = None
    if filename == '-':
      import sys
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
    try:
      pid = int(self.load_text(pidfile).strip())
    except:
      self.write_stderr("Error reading pidfile -- exiting\n")
      sys.exit(1)
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
      elif processevent.event in ('STOPPED', 'STOPPING'):
        t = config["wait_stopped"]
        if t != 0:
          limit_dt = processevent.dt + datetime.timedelta(seconds=t)
          if limit_dt < dt:
            return True
        return False

      # Inspect if expectedly exited processed have extended their stop time allowance
      elif processevent.event == 'EXITED' and processevent.expected is True:
        t = config["wait_expected"]
        if t != 0:
          limit_dt = processevent.dt + datetime.timedelta(seconds=t)
          if limit_dt >= dt:
            return False
        return True      

      # Inspect other (unexpected) exits ('BACKOFF', 'EXITED', 'FATAL', 'UNKNOWN')
      else:
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
          self.write_stderr("Process states that are violating required state:\n")
          self.write_stderr("Exiting.\n")
          self.write_stderr(json.dumps(lst, indent=4)+"\n")
          # Use SIGUSR1 internally to signal our main thread to start exiting
          self.signalq.put(signal.SIGUSR1)

        # Wait for new event
        event = self.eventq.get(timeout=1)

        # We got an event
        rex = re.match('^PROCESS_STATE_(.+)$', event["headers"]["eventname"])
        if rex:
          if "expected" in event["payload"]:
            expected = (event["payload"]["expected"] == "1")
          else: 
            expected = None
          pname = event["payload"]["processname"]
          status[pname] = ProcessEvent(
            dt       = event["dt"],
            process  = pname,
            event    = rex.groups()[0],
            expected = expected 
          )
        elif event["headers"]["eventname"] == "TICK_60":
          last_tick = datetime.datetime.now()

      except queue.Empty:
        pass


  def signal_handler(self, signum, frame):
    sig = signal.Signals(signum)
    self.signalq.put_nowait(sig)


  def run(self):
    class StartExit(Exception):
      pass

    for sig in self.exit_signals:
      signal.signal(sig, self.signal_handler)

    try:
      while True:
        try:
          sig = self.signalq.get(timeout=1)
          if sig in self.exit_signals or sig == signal.SIGUSR1:
            # We will exit with nonzero exitcode
            # and kill the supervisor with us
            raise StartExit
          elif sig in self.stop_signals:
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