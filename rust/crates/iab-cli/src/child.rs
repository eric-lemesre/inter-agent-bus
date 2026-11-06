//! A command run with its input on stdin, its two output streams
//! collected, waited for in slices so the caller can renew a lease or give
//! up — what `subprocess.Popen(...).communicate(input, timeout)` does for
//! worker.py and wake.py. Bytes cross the pipes untouched: no encoding,
//! no line-ending translation (`CONTRAT.md`, § 9).

use std::io::{self, Read, Write};
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

/// How often a running child is looked at.
const TICK: Duration = Duration::from_millis(50);

/// What a finished command left behind.
#[derive(Debug)]
pub struct Finished {
    /// Exit code — negative for a signal on Unix, as Python's
    /// `returncode`; `None` when killed because time ran out.
    pub code: Option<i32>,
    /// Standard output, UTF-8, an invalid byte shown as U+FFFD.
    pub out: String,
    /// Standard error, likewise.
    pub err: String,
}

/// A started command.
pub struct Running {
    child: Child,
    feeder: JoinHandle<()>,
    stdout: JoinHandle<Vec<u8>>,
    stderr: JoinHandle<Vec<u8>>,
}

fn drain(mut source: impl Read + Send + 'static) -> JoinHandle<Vec<u8>> {
    thread::spawn(move || {
        let mut bytes = Vec::new();
        // A read failure leaves what was read: the output is a report,
        // not a reason to lose the task.
        let _ = source.read_to_end(&mut bytes);
        bytes
    })
}

/// Start `argv` in `cwd` (the current directory when `None`) with `input`
/// on its standard input, closed once written.
pub fn start(argv: &[String], cwd: Option<&Path>, input: Vec<u8>) -> io::Result<Running> {
    let (program, args) = argv
        .split_first()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "empty command"))?;
    let mut command = Command::new(program);
    command
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(cwd) = cwd {
        command.current_dir(cwd);
    }
    let mut child = command.spawn()?;
    let (Some(mut stdin), Some(stdout), Some(stderr)) =
        (child.stdin.take(), child.stdout.take(), child.stderr.take())
    else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(io::Error::other("child pipes unavailable"));
    };
    // A command that exits without reading all its input closes the pipe:
    // not an error, communicate() ignores it too.
    let feeder = thread::spawn(move || {
        let _ = stdin.write_all(&input);
    });
    Ok(Running {
        child,
        feeder,
        stdout: drain(stdout),
        stderr: drain(stderr),
    })
}

/// Python's `returncode`: the exit code, or minus the signal.
fn code_of(status: ExitStatus) -> Option<i32> {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        status.code().or_else(|| status.signal().map(|signal| -signal))
    }
    #[cfg(not(unix))]
    {
        status.code()
    }
}

impl Running {
    /// Wait at most `slice`; the status if the command ended meanwhile.
    pub fn wait_for(&mut self, slice: Duration) -> io::Result<Option<ExitStatus>> {
        let until = Instant::now() + slice;
        loop {
            if let Some(status) = self.child.try_wait()? {
                return Ok(Some(status));
            }
            let left = until.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Ok(None);
            }
            thread::sleep(left.min(TICK));
        }
    }

    /// The outputs of an ended command.
    pub fn finish(self, status: ExitStatus) -> Finished {
        self.collect(code_of(status))
    }

    /// Kill the command, then collect what it wrote.
    pub fn kill(mut self) -> Finished {
        let _ = self.child.kill();
        let _ = self.child.wait();
        self.collect(None)
    }

    fn collect(self, code: Option<i32>) -> Finished {
        let _ = self.feeder.join();
        let text = |handle: JoinHandle<Vec<u8>>| {
            String::from_utf8_lossy(&handle.join().unwrap_or_default()).into_owned()
        };
        Finished {
            code,
            out: text(self.stdout),
            err: text(self.stderr),
        }
    }
}
