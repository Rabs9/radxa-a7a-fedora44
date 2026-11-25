//! MCP servers: programs that give the Assistant more tools, spoken to over their stdin and stdout (the Model Context
//! Protocol's stdio transport: one JSON-RPC message a line). The client is the least the Assistant needs: initialize,
//! tools/list, tools/call; a request of the server's (roots, sampling) is answered "not supported", so it never
//! waits on us. A server runs as the owner, with the owner's rights: the tab says so, and adds none by itself.
//!
//! The panel keeps the servers it started (a Pool): an MCP server on npx or uvx takes seconds to start, too long for
//! every question. `a7a-panel --answer` starts its own and stops them as it ends. A server is a process group of its
//! own (npx, uvx and `sh -c` start the real server as a child): it is stopped as one, its stdin closed first (most end
//! on that), then TERM, then KILL. One that did not start is not tried again for five minutes (a question would
//! otherwise wait for it every time), unless the settings page asks for its tools.

use crate::prefs::{self, Server};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::process::CommandExt;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// A question's start of a server: one already downloaded starts in seconds.
pub const START: Duration = Duration::from_secs(60);
/// The settings page's "Its tools": a first start may download the server (npx, uvx).
pub const FIRST_START: Duration = Duration::from_secs(600);
const LIST: Duration = Duration::from_secs(30);
const CALL: Duration = Duration::from_secs(120);
/// A tool's description, as the model reads it in every question: long ones are cut (the board's model has 8192
/// tokens for everything).
const DESCRIPTION: usize = 300;
/// tools/list pages read at most (a server that answers every page with another cursor would loop forever: review)
const PAGES: usize = 20;
/// One message's length at most (a tool's answer reaches the model cut to a few thousand characters anyway); a
/// longer one fails the request it may answer, at once and saying why.
const LINE: usize = 1 << 20;
/// What the reader passes on for a line longer than LINE (no JSON can start with a NUL).
const TOO_LONG: &str = "\0too long";
/// What a server last printed on its stderr, kept for an error message.
const ERR_TAIL: usize = 16 << 10;
/// Messages waiting to be read (more hold the server up: its stdout fills): at most WAITING x LINE in memory.
const WAITING: usize = 32;
const FORGET: Duration = Duration::from_secs(300);
const PROTOCOL: &str = "2025-06-18";

/// One of a server's tools, as it described itself.
#[derive(Debug, Clone, PartialEq)]
pub struct Tool {
    pub name: String,
    pub description: String,
    /// its arguments, a JSON schema
    pub schema: Value,
}

pub struct Client {
    child: Option<Child>,
    stdin: Option<ChildStdin>,
    lines: Receiver<String>,
    /// set when its stdout ends (or fails): the process may live on, but it is no use any more
    dead: Arc<AtomicBool>,
    err: Arc<Mutex<Vec<u8>>>,
    next: u64,
    pub tools: Vec<Tool>,
}

impl Drop for Client {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

/// One line of at most LINE bytes (the rest of a longer one skipped); 0 at the end.
fn read_line(r: &mut impl BufRead, buf: &mut Vec<u8>) -> std::io::Result<usize> {
    let n = r.by_ref().take(LINE as u64).read_until(b'\n', buf)?;
    if n == LINE && buf.last() != Some(&b'\n') {
        let mut skip = Vec::new();
        loop {
            skip.clear();
            let m = r.by_ref().take(1 << 16).read_until(b'\n', &mut skip)?;
            if m == 0 || skip.last() == Some(&b'\n') {
                break;
            }
        }
    }
    Ok(n)
}

impl Client {
    /// Starts the server and asks for its tools, `start` given to its answer to initialize.
    pub fn start(server: &Server, start: Duration) -> Result<Client, String> {
        let argv = prefs::split_command(&server.command)?;
        let (prog, args) = argv.split_first().ok_or("no command")?;
        let env = prefs::env_pairs(&server.env)?;
        let mut child = Command::new(prog)
            .args(args)
            .envs(env)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0)
            .spawn()
            .map_err(|e| format!("{prog}: {e}"))?;
        let stdin = child.stdin.take().ok_or("no stdin")?;
        let stdout = child.stdout.take().ok_or("no stdout")?;
        let mut stderr = child.stderr.take().ok_or("no stderr")?;
        let (tx, lines) = mpsc::sync_channel(WAITING);
        let dead = Arc::new(AtomicBool::new(false));
        let gone = dead.clone();
        // the reader ends at the server's end, or at a read that fails: the channel then says so to whoever waits.
        // Lossy: a stray byte that is no UTF-8 must not end it (review)
        std::thread::spawn(move || {
            let mut r = BufReader::new(stdout);
            let mut buf = Vec::new();
            loop {
                buf.clear();
                match read_line(&mut r, &mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        let line = if n == LINE && buf.last() != Some(&b'\n') {
                            TOO_LONG.to_string()
                        } else {
                            String::from_utf8_lossy(&buf).trim_end_matches(['\r', '\n']).to_string()
                        };
                        if tx.send(line).is_err() {
                            break;
                        }
                    }
                }
            }
            gone.store(true, Ordering::SeqCst);
        });
        let err = Arc::new(Mutex::new(Vec::new()));
        let tail = err.clone();
        std::thread::spawn(move || {
            let mut chunk = [0u8; 4096];
            while let Ok(n) = stderr.read(&mut chunk) {
                if n == 0 {
                    break;
                }
                let mut e = tail.lock().unwrap();
                e.extend_from_slice(&chunk[..n]);
                if e.len() > ERR_TAIL {
                    let cut = e.len() - ERR_TAIL;
                    e.drain(..cut);
                }
            }
        });
        let mut c = Client { child: Some(child), stdin: Some(stdin), lines, dead, err, next: 1, tools: Vec::new() };
        match c.introduce(start) {
            Ok(()) => Ok(c),
            Err(e) => {
                // stopped before the error goes back: the command line exits right after, and a thread left to stop
                // it would go with it (a server that does not end on its stdin outlived it: review)
                if let Some(t) = c.stop() {
                    let _ = t.join();
                }
                Err(e)
            }
        }
    }

    /// initialize, then the tools (paged).
    fn introduce(&mut self, start: Duration) -> Result<(), String> {
        let c = self;
        c.request("initialize", json!({"protocolVersion": PROTOCOL, "capabilities": {},
                                       "clientInfo": {"name": "a7a-panel", "version": env!("CARGO_PKG_VERSION")}}), start)?;
        c.send(&json!({"jsonrpc": "2.0", "method": "notifications/initialized"}))?;
        let mut cursor: Option<String> = None;
        for _ in 0..PAGES {
            let params = match &cursor {
                Some(c) => json!({"cursor": c}),
                None => json!({}),
            };
            let r = c.request("tools/list", params, LIST)?;
            let before = c.tools.len();
            for t in r["tools"].as_array().cloned().unwrap_or_default() {
                let Some(name) = t["name"].as_str() else { continue };
                c.tools.push(Tool {
                    name: name.to_string(),
                    description: t["description"].as_str().unwrap_or("").chars().take(DESCRIPTION).collect(),
                    schema: if t["inputSchema"].is_object() { t["inputSchema"].clone() } else { json!({"type": "object", "properties": {}}) },
                });
            }
            let next = r["nextCursor"].as_str().map(str::to_string);
            // the end: no cursor, a page that brought nothing, or the same cursor again
            if next.is_none() || c.tools.len() == before || next == cursor {
                break;
            }
            cursor = next;
        }
        Ok(())
    }

    /// The last lines the server printed on its stderr: why it did not start, usually.
    fn tail(&self) -> String {
        let e = self.err.lock().unwrap();
        let s = String::from_utf8_lossy(&e);
        let lines: Vec<&str> = s.lines().filter(|l| !l.trim().is_empty()).collect();
        lines[lines.len().saturating_sub(3)..].join(" | ").chars().take(400).collect()
    }

    /// A message to the server; one that has ended says why, as far as its stderr tells.
    fn send(&mut self, msg: &Value) -> Result<(), String> {
        let ok = match self.stdin.as_mut() {
            Some(stdin) => writeln!(stdin, "{msg}").and_then(|_| stdin.flush()).is_ok(),
            None => false,
        };
        if ok {
            Ok(())
        } else {
            Err(self.ended())
        }
    }

    fn ended(&mut self) -> String {
        // its stderr's last words come a moment after its stdout closed
        std::thread::sleep(Duration::from_millis(100));
        let code = self.child.as_ref().and_then(exited).map(|s| format!(" ({s})")).unwrap_or_default();
        let why = self.tail();
        format!("the server ended{code}{}", if why.is_empty() { String::new() } else { format!(": {why}") })
    }

    /// One request, and its answer: what the server sends meanwhile (notifications, its own requests) is dealt with.
    fn request(&mut self, method: &str, params: Value, timeout: Duration) -> Result<Value, String> {
        let id = self.next;
        self.next += 1;
        self.send(&json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}))?;
        let end = Instant::now() + timeout;
        loop {
            let left = end.saturating_duration_since(Instant::now());
            let line = match self.lines.recv_timeout(left) {
                Ok(l) => l,
                Err(RecvTimeoutError::Timeout) => return Err(format!("no answer to {method} in {} s", timeout.as_secs())),
                Err(RecvTimeoutError::Disconnected) => return Err(self.ended()),
            };
            if line == TOO_LONG {
                return Err(format!("the server sent a message longer than {} MB", LINE >> 20));
            }
            let Ok(m) = serde_json::from_str::<Value>(&line) else { continue }; // a server's stray print
            if m.get("method").is_some() {
                if let Some(rid) = m.get("id") {
                    // the server asks us something (roots, sampling, ping): ping is answered, the rest refused
                    let reply = if m["method"] == "ping" {
                        json!({"jsonrpc": "2.0", "id": rid, "result": {}})
                    } else {
                        json!({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "not supported by this client"}})
                    };
                    self.send(&reply)?;
                }
                continue;
            }
            if m["id"].as_u64() != Some(id) {
                continue;
            }
            if let Some(e) = m.get("error") {
                return Err(e["message"].as_str().unwrap_or("an error").to_string());
            }
            return Ok(m["result"].clone());
        }
    }

    /// One tool call: its text (an image or other content named, not shown), or the server's error.
    pub fn call(&mut self, tool: &str, args: &Value) -> Result<String, String> {
        let r = self.request("tools/call", json!({"name": tool, "arguments": args}), CALL)?;
        let mut parts = Vec::new();
        for c in r["content"].as_array().cloned().unwrap_or_default() {
            match c["type"].as_str() {
                Some("text") => parts.push(c["text"].as_str().unwrap_or("").to_string()),
                Some("resource") => parts.push(c["resource"]["text"].as_str().map(str::to_string)
                    .unwrap_or_else(|| format!("[resource {}]", c["resource"]["uri"].as_str().unwrap_or("")))),
                Some(kind) => parts.push(format!("[{kind}]")),
                None => {}
            }
        }
        if parts.is_empty() && r.get("structuredContent").is_some() {
            parts.push(r["structuredContent"].to_string());
        }
        let text = parts.join("\n");
        if r["isError"].as_bool() == Some(true) {
            return Err(if text.is_empty() { "the tool failed".into() } else { text });
        }
        Ok(text)
    }

    pub fn alive(&mut self) -> bool {
        !self.dead.load(Ordering::SeqCst) && self.child.as_ref().is_some_and(|c| exited(c).is_none())
    }

    /// Stops the server's process group: its stdin closed (most servers end on that), a second later TERM, a second
    /// after that KILL; on a thread of its own, which the caller may wait for (the command line does, as it exits).
    pub fn stop(&mut self) -> Option<std::thread::JoinHandle<()>> {
        let stdin = self.stdin.take();
        let mut child = self.child.take()?;
        Some(std::thread::spawn(move || {
            drop(stdin);
            let group = child.id() as libc::pid_t;
            // the leader is waited for only after the group's signals: reaped earlier, its number (the group's)
            // could be another process's by the time they go (review)
            let ended = |c: &Child| {
                let until = Instant::now() + Duration::from_secs(1);
                while Instant::now() < until && exited(c).is_none() {
                    std::thread::sleep(Duration::from_millis(50));
                }
            };
            ended(&child);
            // the group: the real server under npx or sh -c too (one that ended with its stdin leaves none)
            unsafe { libc::killpg(group, libc::SIGTERM) };
            ended(&child);
            unsafe { libc::killpg(group, libc::SIGKILL) };
            let _ = child.wait();
        }))
    }
}

/// How the process ended, when it has (a zombie now), without reaping it: its number, and its group's, stay ours
/// until stop() waits for it.
fn exited(child: &Child) -> Option<String> {
    let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
    let r = unsafe { libc::waitid(libc::P_PID, child.id() as libc::id_t, &mut info, libc::WEXITED | libc::WNOHANG | libc::WNOWAIT) };
    if r != 0 {
        // reaped already (by a wait elsewhere): ended, how is no longer known
        return (std::io::Error::last_os_error().raw_os_error() == Some(libc::ECHILD)).then(|| "ended".to_string());
    }
    if unsafe { info.si_pid() } == 0 {
        return None;
    }
    let status = unsafe { info.si_status() };
    Some(if info.si_code == libc::CLD_EXITED { format!("exit status: {status}") } else { format!("signal: {status}") })
}

/// The servers the panel has started, by what they were started as (a changed command or environment is another
/// one), and those that did not start lately.
#[derive(Default)]
pub struct Pool {
    clients: Mutex<HashMap<String, Arc<Mutex<Client>>>>,
    failed: Mutex<HashMap<String, (Instant, String)>>,
}

/// A server as the pool knows it: its name, command and environment (another of any of them is another server).
pub fn key(s: &Server) -> String {
    format!("{}\n{}\n{}", s.name, s.command, s.env)
}

impl Pool {
    /// The server, started when it is not running (or has ended), `start` given to its start. One that did not
    /// start in the last five minutes gives that failure again at once (forget() first to try it anew).
    pub fn get(&self, server: &Server, start: Duration) -> Result<Arc<Mutex<Client>>, String> {
        let k = key(server);
        // the pool's lock let go before the server's is taken: a server busy with a long call must not hold up the
        // others, nor the window (retain and stop_all run on it)
        let running = self.clients.lock().unwrap().get(&k).cloned();
        if let Some(c) = running {
            if c.lock().unwrap().alive() {
                return Ok(c);
            }
        }
        if let Some((at, e)) = self.failed.lock().unwrap().get(&k).cloned() {
            if at.elapsed() < FORGET {
                return Err(e);
            }
        }
        // started outside the pool's lock: a slow start does not hold up the others
        match Client::start(server, start) {
            Ok(c) => {
                let c = Arc::new(Mutex::new(c));
                self.clients.lock().unwrap().insert(k, c.clone());
                Ok(c)
            }
            Err(e) => {
                self.failed.lock().unwrap().insert(k, (Instant::now(), e.clone()));
                Err(e)
            }
        }
    }

    /// The server is running now (not starting it; busy with a call counts).
    pub fn running(&self, server: &Server) -> bool {
        let c = self.clients.lock().unwrap().get(&key(server)).cloned();
        c.is_some_and(|c| c.try_lock().map(|mut c| c.alive()).unwrap_or(true))
    }

    /// A get() would start the server now: not running, and no failure of it remembered.
    pub fn would_start(&self, server: &Server) -> bool {
        let failed = self.failed.lock().unwrap().get(&key(server)).is_some_and(|(at, _)| at.elapsed() < FORGET);
        !failed && !self.running(server)
    }

    /// The server's last failure forgotten: the next get starts it again.
    pub fn forget(&self, server: &Server) {
        self.failed.lock().unwrap().remove(&key(server));
    }

    /// Stops the servers that are not in `servers` as they are now (removed, changed, switched off). (The failures
    /// stay: retain also runs after every answer.)
    pub fn retain(&self, servers: &[Server]) {
        let keep: Vec<String> = servers.iter().filter(|s| s.on).map(key).collect();
        let gone: Vec<Arc<Mutex<Client>>> = {
            let mut clients = self.clients.lock().unwrap();
            let gone = clients.iter().filter(|(k, _)| !keep.contains(k)).map(|(_, c)| c.clone()).collect();
            clients.retain(|k, _| keep.contains(k));
            gone
        };
        drop(gone); // (each stops as its last user lets go: one busy with a call, after it)
    }

    /// Every failure forgotten: the settings were saved, the next question tries them all again.
    pub fn forget_all(&self) {
        self.failed.lock().unwrap().clear();
    }

    /// Stops them all; with `wait`, until they have ended (the command line, which exits next and would take the
    /// stopping threads with it).
    pub fn stop_all(&self, wait: bool) {
        let all: Vec<Arc<Mutex<Client>>> = self.clients.lock().unwrap().drain().map(|(_, c)| c).collect();
        let mut threads = Vec::new();
        for c in all {
            if let Ok(m) = Arc::try_unwrap(c) {
                threads.extend(m.into_inner().unwrap().stop());
            }
        }
        if wait {
            for t in threads {
                let _ = t.join();
            }
        }
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;

    /// A server speaking MCP the way real ones do (a Python script): two tools, one that fails, a request of its
    /// own before an answer, a stray print (and a stray byte that is no UTF-8), and a tool that takes long.
    pub fn fake_server(dir: &std::path::Path, name: &str) -> Server {
        let script = dir.join("fake-mcp.py");
        std::fs::write(&script, r#"
import json, sys, time
def send(m): sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
print("starting up (a stray print)", flush=True)
sys.stdout.buffer.write(b"caf\xe9 (no UTF-8)\n"); sys.stdout.flush()
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m: continue
    if m.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": 900, "method": "roots/list"})          # ours to refuse
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}})
    elif m.get("method") == "tools/list":
        if m.get("params", {}).get("cursor") is None:
            send({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [{"name": "echo", "description": "Says it back." + " x" * 400, "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}], "nextCursor": "2"}})
        else:
            send({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [{"name": "fail"}, {"name": "slow", "description": "Takes long."}]}})
    elif m.get("method") == "tools/call":
        name, args = m["params"]["name"], m["params"].get("arguments", {})
        send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
        if name == "echo": send({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": "you said " + args.get("text", "")}, {"type": "image", "data": "", "mimeType": "image/png"}]}})
        elif name == "fail": send({"jsonrpc": "2.0", "id": m["id"], "result": {"isError": True, "content": [{"type": "text", "text": "it broke"}]}})
        elif name == "slow": time.sleep(5)
        else: send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32602, "message": "unknown tool " + name}})
    elif m.get("id") == 900: pass
"#).unwrap();
        Server { name: name.into(), command: format!("python3 {}", script.display()), env: "FAKE_TOKEN=1".into(), reach: prefs::Reach::Board, on: true, off_tools: vec![] }
    }

    pub fn scratch(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("a7a-mcp-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn a_server_starts_lists_its_tools_and_answers() {
        let d = scratch("start");
        let mut c = Client::start(&fake_server(&d, "fake-start"), START).unwrap();
        let names: Vec<&str> = c.tools.iter().map(|t| t.name.as_str()).collect();
        assert_eq!(names, ["echo", "fail", "slow"]); // both pages of the list
        assert_eq!(c.tools[0].description.chars().count(), DESCRIPTION);
        assert_eq!(c.tools[1].schema, json!({"type": "object", "properties": {}}));
        assert_eq!(c.call("echo", &json!({"text": "hi"})).unwrap(), "you said hi\n[image]");
        assert_eq!(c.call("fail", &json!({})).unwrap_err(), "it broke");
        assert_eq!(c.call("nope", &json!({})).unwrap_err(), "unknown tool nope");
        assert!(c.alive()); // the stray byte that is no UTF-8 did not end it
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn a_server_that_is_not_there_or_ends_is_said_so() {
        let d = scratch("missing");
        let mut s = fake_server(&d, "fake-missing");
        s.command = "no-such-mcp-server --stdio".into();
        assert!(Client::start(&s, START).err().unwrap().starts_with("no-such-mcp-server: "));
        s.command = "sh -c 'echo cannot find the package >&2; exit 3'".into();
        let e = Client::start(&s, START).err().unwrap();
        assert!(e.contains("the server ended") && e.contains("cannot find the package"), "{e}");
        s.command = "python3 'unclosed".into();
        assert_eq!(Client::start(&s, START).err().unwrap(), "a ' is not closed");
        s.command = "python3 x.py".into();
        s.env = "NOT AN ENV".into();
        assert!(Client::start(&s, START).err().unwrap().contains("is not KEY=value"));
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn endless_pages_and_silence_end() {
        let d = scratch("endless");
        let script = d.join("endless.py");
        std::fs::write(&script, r#"
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "initialize": print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {}}), flush=True)
    elif m.get("method") == "tools/list":
        n = int(m.get("params", {}).get("cursor") or 0)
        tools = [{"name": "t%d" % n}] if n < 50 else []
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": tools, "nextCursor": str(n + 1)}}), flush=True)
"#).unwrap();
        let s = Server { name: "endless".into(), command: format!("python3 {}", script.display()), ..Default::default() };
        let c = Client::start(&s, START).unwrap();
        assert_eq!(c.tools.len(), PAGES); // twenty pages, not fifty (nor forever)
        let silent = Server { name: "silent".into(), command: "sleep 30".into(), ..Default::default() };
        let t = Instant::now();
        assert_eq!(Client::start(&silent, Duration::from_secs(1)).err().unwrap(), "no answer to initialize in 1 s");
        assert!(t.elapsed() < Duration::from_secs(5));
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn a_server_and_its_children_are_stopped_together() {
        // `sh` starts the real work as a child that does not end with its stdin: TERM to the group ends it too
        let d = scratch("group");
        let pidfile = d.join("child.pid");
        let script = d.join("server.sh");
        std::fs::write(&script, format!(r#"sleep 300 & echo $! > {}
read line; echo '{{"jsonrpc": "2.0", "id": 1, "result": {{}}}}'
read line; read line; echo '{{"jsonrpc": "2.0", "id": 2, "result": {{"tools": []}}}}'
wait
"#, pidfile.display())).unwrap();
        let s = Server { name: "group".into(), command: format!("sh {}", script.display()), ..Default::default() };
        let mut c = Client::start(&s, START).unwrap();
        let child: i32 = std::fs::read_to_string(&pidfile).unwrap().trim().parse().unwrap();
        assert_eq!(unsafe { libc::kill(child, 0) }, 0);
        c.stop().unwrap().join().unwrap();
        let until = Instant::now() + Duration::from_secs(3);
        while unsafe { libc::kill(child, 0) } == 0 && Instant::now() < until {
            std::thread::sleep(Duration::from_millis(50)); // (reaped by whoever adopted it)
        }
        assert_ne!(unsafe { libc::kill(child, 0) }, 0, "the server's child outlived it");
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn a_message_too_long_fails_at_once_and_a_server_that_dies_is_replaced() {
        let d = scratch("long");
        let script = d.join("long.py");
        std::fs::write(&script, r#"
import json, sys, os
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m: continue
    if m.get("method") == "initialize": print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {}}), flush=True)
    elif m.get("method") == "tools/list": print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [{"name": "big"}, {"name": "die"}]}}), flush=True)
    elif m["params"]["name"] == "big": print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": "x" * (2 << 20)}]}}), flush=True)
    else: os._exit(9)
"#).unwrap();
        let s = Server { name: "long".into(), command: format!("python3 {}", script.display()), ..Default::default() };
        let pool = Pool::default();
        let c = pool.get(&s, START).unwrap();
        let t = Instant::now();
        assert_eq!(c.lock().unwrap().call("big", &json!({})).unwrap_err(), "the server sent a message longer than 1 MB");
        assert!(t.elapsed() < Duration::from_secs(10), "it waited out its timeout");
        let e = c.lock().unwrap().call("die", &json!({})).unwrap_err();
        assert!(e.starts_with("the server ended"), "{e}");
        assert!(!c.lock().unwrap().alive());
        assert!(!Arc::ptr_eq(&c, &pool.get(&s, START).unwrap())); // the next question gets a new one
        pool.stop_all(true);
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn a_server_that_did_not_start_is_gone_when_start_returns() {
        // it ignores the end of its stdin and never answers: stopped (TERM to its group) before the error comes back,
        // so the command line, which exits next, leaves nothing behind (review)
        let d = scratch("nostart");
        let pidfile = d.join("pid");
        let s = Server { name: "nostart".into(), command: format!("sh -c 'trap \"\" HUP; echo $$ > {}; exec sleep 300'", pidfile.display()), ..Default::default() };
        assert!(Client::start(&s, Duration::from_secs(1)).is_err());
        let pid: i32 = std::fs::read_to_string(&pidfile).unwrap().trim().parse().unwrap();
        assert_ne!(unsafe { libc::kill(pid, 0) }, 0, "still running after start gave up");
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn the_pool_keeps_a_server_starts_a_changed_one_anew_and_remembers_a_failure() {
        let d = scratch("pool");
        let pool = Pool::default();
        let s = fake_server(&d, "fake-pool");
        assert!(!pool.running(&s));
        let a = pool.get(&s, START).unwrap();
        assert!(pool.running(&s));
        assert!(Arc::ptr_eq(&a, &pool.get(&s, START).unwrap()));
        let mut changed = s.clone();
        changed.env = "FAKE_TOKEN=2".into();
        assert!(!Arc::ptr_eq(&a, &pool.get(&changed, START).unwrap()));
        pool.retain(&[changed.clone()]);
        assert_eq!(pool.clients.lock().unwrap().len(), 1);
        changed.on = false;
        pool.retain(&[changed]);
        assert!(pool.clients.lock().unwrap().is_empty());
        // one that ended is started again
        let b = pool.get(&s, START).unwrap();
        b.lock().unwrap().child.as_mut().unwrap().kill().unwrap();
        b.lock().unwrap().child.as_mut().unwrap().wait().unwrap();
        assert!(!Arc::ptr_eq(&b, &pool.get(&s, START).unwrap()));
        // one that did not start is not tried again at once (each question would wait for it), until forgotten
        let mut broken = s.clone();
        broken.command = "sh -c 'exit 1'".into();
        let first = pool.get(&broken, START).err().unwrap();
        let t = Instant::now();
        assert_eq!(pool.get(&broken, START).err().unwrap(), first);
        assert!(t.elapsed() < Duration::from_millis(50), "started again");
        pool.forget(&broken);
        assert!(pool.get(&broken, START).is_err());
        pool.retain(&[s.clone()]); // after an answer: the failures stay
        assert!(!pool.failed.lock().unwrap().is_empty());
        pool.forget_all(); // a Save
        assert!(pool.failed.lock().unwrap().is_empty());
        pool.stop_all(true);
        assert!(pool.clients.lock().unwrap().is_empty());
        std::fs::remove_dir_all(&d).ok();
    }
}
