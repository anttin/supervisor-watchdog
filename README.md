# supervisor-watchdog

This is a simple watchdog application for supervisord. It acts as a listener that will kill the supervisord process when based on the configuration it is deemed that a container restart is needed. It is useful for running applications in containers under supervisord.

## Install

Clone the repository and install the module dependencies.

```bash
# Clone repository
git clone https://github.com/anttin/supervisor-watchdog.git /usr/local/bin/supervisor-watchdog
cd /usr/local/bin/supervisor-watchdog

# optionally create and activate a virtual environment (typically not useful for container deployments)
python3 -m venv venv-swd
source venv-swd/bin/activate

# install dependencies
python3 -m pip install -r /usr/local/bin/supervisor-watchdog/requirements.txt
```

Example for a Dockerfile (ensure you have python3, pip, and git installed before this):

```dockerfile
RUN git clone https://github.com/anttin/supervisor-watchdog.git /usr/local/bin/supervisor-watchdog
RUN python3 -m pip install -r /usr/local/bin/supervisor-watchdog/requirements.txt
```

## Usage

Configure the `supervisord.conf`. In addition to other configuration needed for your application, you will need to configure the pidfile for the supervisod-section, and the listener configuration. Example shown here:

```ini
[supervisord]
nodaemon=true
pidfile=/var/run/supervisord.pid

[eventlistener:watchdog]
command=/usr/bin/python3 /usr/local/bin/supervisor-watchdog/watchdog.py -s -p /var/run/supervisord.pid
events=PROCESS_STATE_STOPPED,PROCESS_STATE_EXITED,PROCESS_STATE_UNKNOWN,PROCESS_STATE_FATAL,PROCESS_STATE,TICK_60
stderr_logfile=/var/log/supervisor-watchdog.log
stopsignal=QUIT
```

You add command line options for the watchdog's command-configuration in order to configure the watchdog.

The mandatory parameters are:

```text
-p|--pidfile <path-to-supervisord-pidfile>
```

The optional configuration options are:

```text
-w|--waitu     <seconds>
-W|--waite     <seconds>
-S|--waits     <seconds>
-n|--notick
-s|--silent
```

`pidfile` is the file path to supervisord's pidfile.

`waitu` is the time in seconds to wait for an unexpectedly exited process (unexpectedness depends on supervisord configuration, default is that it is unexpected if return code != 0) to come back up. Set this to a value to something in order to wait for a restart done by supervisord. Default value is zero, which causes the watchdog to kill the supervisord immediately whenver a process exits unexpectedly.

`waite` is the time in seconds to wait for an expectedly exited process to come back up. Set this to a value to something in order to wait for a restart done by supervisord. Default value is zero, which causes the watchdog to kill the supervisord immediately whenver a process exits expectedly.

`waits` is the time in seconds to wait for a stopped process to come back up. Set this to a value to something in order to ensure the watchdog exits if a process stays stopped too long. Default value is zero, which causes the watchdog to never kill the supervisord for a properly stopped process.

`notick` is a flag that when set overrides the automatic kill of supervisord if the 60 second tick messages stop coming.

`silent` is a flag which when set, ensures that the application does not log events to the stderr-log.
