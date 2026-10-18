import time
import json
import subprocess
import signal
import sys

# run the bpftrace script and capture its output in JSON format

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
run_id = sys.argv[1] if len(sys.argv) > 1 else 'default'
writeFile = f'data/probeData_{run_id}.jsonl'

os.makedirs('data', exist_ok=True)


def load_boot_epoch(rid: str) -> float:
    """boot_time_epoch recorded by parentProcess.py for this run (fallback: estimate)."""
    meta_path = f'data/meta_{rid}.json'
    try:
        with open(meta_path, 'r') as f:
            return float(json.load(f)['boot_time_epoch'])
    except Exception:
        return time.time() - time.monotonic()


def extract_ns_marker(d: dict, prefix: str):
    """Read 'PREFIX <nsecs>' marker lines emitted by probes.bt (BEGIN_NS / WINDOW_END_NS).

    With `-f json`, printf output arrives wrapped (e.g. {"type": "printf", "data": "..."}),
    so search any string field for the prefix.
    """
    for value in d.values():
        if isinstance(value, str) and value.startswith(prefix):
            try:
                return int(value[len(prefix):].strip())
            except ValueError:
                return None
    return None


def merge_lines_in_map(probeData: list, window_start, window_end):
    """Merge the map lines of one window into a single record."""
    # structure of the output row
    row = {'window_start': window_start, 'window_end': window_end, 'processes': {}}
    for currData in probeData:
        for param, data in currData.get('data', {}).items():
            metric = param.lstrip('@')
            if isinstance(data, dict):
                for comm, value in data.items():
                    row['processes'].setdefault(comm, {})[metric] = value
            else:
                row[metric] = data
    return row


class ProbeStreamParser:
    """Parses bpftrace probes.bt output into windowed JSONL rows.

    Window boundaries come from the bpftrace clock (BEGIN_NS / WINDOW_END_NS
    markers printed by probes.bt) converted with the run's boot_time_epoch.
    Falls back to wall-clock time when markers are missing.
    """

    def __init__(self, boot_epoch: float, write_fn):
        self.boot_epoch = boot_epoch
        self.write_fn = write_fn
        self.init_timestamp = None       # end of the previous window (epoch)
        self.begin_ns = None             # attach time (boot-relative)
        self.window_end_ns = None        # interval boundary (boot-relative)
        self.curr_probes = []
        self.windows_written = 0

    def handle(self, line: str) -> bool:
        line = line.strip()
        if line == '':
            return False
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            # ignore non-json lines like "Attaching 12 probes..."
            return False
        if not isinstance(d, dict):
            return False

        if d.get('data') == 'END':
            self._flush()
            return True

        ns = extract_ns_marker(d, 'WINDOW_END_NS ')
        if ns is not None:
            self.window_end_ns = ns
            return False
        ns = extract_ns_marker(d, 'BEGIN_NS ')
        if ns is not None:
            self.begin_ns = ns
            return False

        if d.get('type') == 'map':
            self.curr_probes.append(d)
        return False

    def _flush(self):
        if self.window_end_ns is not None:
            fin_timestamp = self.boot_epoch + (self.window_end_ns / 1e9)
        else:
            fin_timestamp = time.time()
        if self.init_timestamp is not None:
            win_start = self.init_timestamp
        elif self.begin_ns is not None:
            win_start = self.boot_epoch + (self.begin_ns / 1e9)
        else:
            # legacy fallback: assume the window covered the last 3 seconds
            win_start = fin_timestamp - 3.0
        currLine = merge_lines_in_map(self.curr_probes, win_start, fin_timestamp)
        self.write_fn(currLine)
        self.init_timestamp = fin_timestamp
        self.window_end_ns = None
        self.curr_probes = []
        self.windows_written += 1


def main():
    boot_epoch = load_boot_epoch(run_id)

    # clear the output file before writing to it
    with open(writeFile, 'w'):
        pass

    print("Reading Probes")
    proc = subprocess.Popen(
        ['bpftrace', '-q', '-f', 'json', 'probes.bt'],
        stdout=subprocess.PIPE,
        text=True,
    )

    with open(writeFile, 'a') as op:
        def write_row(row):
            op.write(json.dumps(row))
            op.write('\n')
            op.flush()

        parser = ProbeStreamParser(boot_epoch, write_row)

        def consume(stream):
            for line in stream:
                parser.handle(line)

        try:
            consume(proc.stdout)
        except KeyboardInterrupt:
            # continue reading lines if any exist after the script is interrupted
            proc.send_signal(signal.SIGINT)
            consume(proc.stdout)

    # wait until process has completed
    proc.wait()
    print(f"Probe Extraction Done! ({parser.windows_written} windows -> {writeFile})")


if __name__ == '__main__':
    main()
