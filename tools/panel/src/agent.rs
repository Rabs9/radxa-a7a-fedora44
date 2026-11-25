//! The Assistant: a chat with the local model (llama.cpp's llama-server on 127.0.0.1:8080, started by `a7a-llm
//! serve`), which may look at this board through read-only tools. The protocol and tools of the old Python chat window
//! (a7a-llm-gui, removed 2026-11-24): OpenAI-style chat completions, streamed, native tool calls (the server runs with
//! --jinja), at most six tool rounds per question.
//!
//! The model reads local files and reads the web, and a web page (or a local file) can carry instructions; a URL, a
//! search query or a link in the answer is enough to send data out. So (security reviews, 2026-11-17): a chat uses
//! this board's tools or the web, never both: once it has looked at anything local, its web tools pause, and once it
//! has been on the web, its local tools pause, until a new chat; links in a chat that has read local data are not
//! opened. The web tools reach public internet addresses only (not this board's networks, IPv6 included), connecting
//! to the very address that was checked (one lookup: no DNS rebinding) and judging every redirect again. Files are
//! judged by what their path really points to, and only paths that exist are passed on to a program.

use crate::{mcp, prefs};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read};
use std::net::{IpAddr, SocketAddr, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

/// The model server: llama-server on this board, on a7a-llm's port.
pub fn api() -> String {
    format!("http://127.0.0.1:{}", std::env::var("A7A_LLM_PORT").unwrap_or_else(|_| "8080".into()))
}
const MAX_READ: usize = 3000;
const MAX_WEB: usize = 3500;
const MAX_OBS: usize = 3500;
const MAX_TOOL_ROUNDS: usize = 6;
const UA: &str = "Mozilla/5.0 (X11; Linux aarch64) a7a-panel";

/// What the model is told first, before the tools it has and the owner's instructions are added (system_prompt).
pub const SYSTEM_PROMPT: &str = "Local assistant on a Radxa Cubie A7A (Allwinner A733, Fedora 44).";

/// The whole system prompt of a question: who it is, the tools it is offered (call one before answering anything about
/// this machine's state or any specific product, spec or current fact), and the owner's instructions from the
/// Assistant's settings.
pub fn system_prompt(tools: &Tools) -> String {
    let names: Vec<String> = schemas(&tools.settings, &tools.mcp_tools).iter()
        .filter_map(|t| t["function"]["name"].as_str().map(str::to_string)).collect();
    let mut p = format!("{SYSTEM_PROMPT} You have tools: {}. Call one before answering anything about this machine's state \
or any specific product, spec or current fact. The board's own tools (system_info, list_dir, read_file, search_files) \
only read: they cannot write, delete or run anything.", names.join(", "));
    let own = tools.settings.instructions.trim();
    if !own.is_empty() {
        p.push_str("\n\nThe owner's instructions:\n");
        p.push_str(own);
    }
    p
}

pub const LOCAL_TOOLS: [&str; 4] = ["system_info", "list_dir", "read_file", "search_files"];
pub const WEB_TOOLS: [&str; 3] = ["web_search", "fetch_url", "weather"];

#[derive(Debug, Clone)]
pub enum Event {
    Chunk(String),
    Tool { audit: String, observation: String },
    Done { text: String, tok_s: f32, tokens: u32 },
    Fail(String),
    /// something the owner should know that is no answer (an MCP server that did not start)
    Note(String),
}

#[derive(Debug, Clone)]
pub struct Settings {
    pub temperature: f32,
    pub max_tokens: u32,
    pub tools: bool,
    pub web: bool,
    pub searxng: String,
    /// web_search is offered only when the SearXNG answers (`run` asks once per question)
    pub search: bool,
    // the owner's settings (prefs.rs)
    pub instructions: String,
    pub folders: Vec<String>,
    /// the folders setting cannot be used: the file tools read nothing (prefs::Prefs::folders_error)
    pub folders_error: String,
    pub use_search: bool,
    pub use_fetch: bool,
    pub use_weather: bool,
    pub context: u32,
    /// the MCP servers switched on
    pub mcp: Vec<prefs::Server>,
}

impl Default for Settings {
    fn default() -> Self {
        Settings { temperature: 0.3, max_tokens: 512, tools: true, web: true, searxng: "http://127.0.0.1:8888".into(), search: true,
                   instructions: String::new(), folders: Vec::new(), folders_error: String::new(), use_search: true, use_fetch: true, use_weather: true,
                   context: 8192, mcp: Vec::new() }
    }
}

impl Settings {
    /// The owner's settings, with the tab's web switch as it is now.
    pub fn from_prefs(p: &prefs::Prefs, web: bool) -> Settings {
        Settings { max_tokens: p.answer_length, web, instructions: p.instructions.clone(), folders: p.folders.clone(), folders_error: p.folders_error.clone(),
                   use_search: p.web_search, use_fetch: p.fetch_url, use_weather: p.weather, context: p.context,
                   mcp: p.mcp.iter().filter(|s| s.on).cloned().collect(), ..Default::default() }
    }
}

/// One of an MCP server's tools, as the model sees it: "<server>_<tool>" (letters, digits, '_' and '-', 64 at most).
#[derive(Debug, Clone)]
pub struct McpTool {
    pub fname: String,
    pub server: prefs::Server,
    pub tool: mcp::Tool,
}

impl McpTool {
    pub fn new(server: &prefs::Server, tool: &mcp::Tool) -> McpTool {
        let own: String = tool.name.chars().map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' }).collect();
        McpTool { fname: format!("{}_{own}", server.name).chars().take(64).collect(), server: server.clone(), tool: tool.clone() }
    }
}

/// What a chat has looked at: this board, the web. A chat may use one of the two, never both (a new chat, a new Taint).
#[derive(Debug, Default)]
pub struct Taint {
    pub local: AtomicBool,
    pub web: AtomicBool,
    /// the MCP server that both reads this board and reaches the internet this chat uses, alone (prefs::Reach::Both)
    pub both: std::sync::Mutex<Option<String>>,
}

fn schemas(s: &Settings, mcp_tools: &[McpTool]) -> Vec<Value> {
    let f = |name: &str, desc: &str, params: Value| json!({"type": "function", "function": {"name": name, "description": desc, "parameters": params}});
    let one = |key: &str| json!({"type": "object", "properties": {key: {"type": "string"}}, "required": [key]});
    let mut v = vec![
        f("system_info", "Live state of this board: cpu, mem (with the programs using the most memory), disk, temp (every sensor, named), procs (top programs by CPU and by memory), net, os.",
          json!({"type": "object", "properties": {"topic": {"type": "string", "enum": ["cpu", "mem", "disk", "temp", "procs", "net", "os"]}}, "required": ["topic"]})),
        f("list_dir", "List a directory.", one("path")),
        f("read_file", "Read a text file.", one("path")),
        f("search_files", "Grep file contents under a directory.",
          json!({"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern", "path"]})),
    ];
    if s.web {
        if s.search && s.use_search {
            v.push(f("web_search", "Search the web.", one("query")));
        }
        if s.use_fetch {
            v.push(f("fetch_url", "Read a specific web page. Use whenever the user gives a URL.", one("url")));
        }
        if s.use_weather {
            v.push(f("weather", "Weather. Use instead of web_search.", json!({"type": "object", "properties": {"location": {"type": "string"}}})));
        }
    }
    // an MCP server that reaches the internet goes with the web switch, as the web tools do
    for t in mcp_tools.iter().filter(|t| s.web || t.server.reach == prefs::Reach::Board) {
        v.push(f(&t.fname, &t.tool.description, t.tool.schema.clone()));
    }
    v
}

/// How many characters of messages a question may carry (older tool results are shortened past it): the context
/// (`context`: the server's own, which may be older than the settings') less the answer, at about 3 characters a
/// token, less the tools' descriptions and the system prompt (20 000 for 8192 tokens). Too little: see run.
fn budget(s: &Settings, context: u32, schema_chars: usize) -> usize {
    ((context.saturating_sub(s.max_tokens)) as usize * 3).saturating_sub(schema_chars + 2000)
}

/// The context the model server runs with (llama-server's /props), which a new setting reaches only at its next
/// start; the settings' own when the server does not say.
fn running_context(s: &Settings) -> u32 {
    ureq::get(&format!("{}/props", api())).timeout(Duration::from_millis(1500)).call().ok()
        .and_then(|r| r.into_string().ok())
        .and_then(|b| serde_json::from_str::<Value>(&b).ok())
        .and_then(|v| v["default_generation_settings"]["n_ctx"].as_u64())
        .map_or(s.context, |n| n as u32)
}

// ------------------------------------------------------------------ the tools

/// Paths a model must never read: keys, keyrings and wallets, browser and mail profiles (Flatpak ones too), saved
/// network passwords, tokens, shell histories, other programs' environment and command lines.
pub fn secret(path: &str) -> bool {
    secret_beside(path, &prefs::Prefs::path())
}

/// secret, with the Assistant's settings file at `own` (where XDG_CONFIG_HOME puts it, the link resolved too): an
/// MCP server's token or an online assistant's key may be in it.
fn secret_beside(path: &str, own: &Path) -> bool {
    let mut own_dirs: Vec<PathBuf> = own.parent().filter(|d| d.is_absolute()).map(|d| vec![d.to_path_buf()]).unwrap_or_default();
    if let Some(real) = own_dirs.first().and_then(|d| std::fs::canonicalize(d).ok()) {
        own_dirs.push(real);
    }
    let own_dir = own_dirs.iter().map(|d| d.to_string_lossy().into_owned()).any(|d| path == d || path.starts_with(&format!("{d}/")));
    let dirs = ["/.ssh", "/.gnupg", "/.local/share/keyrings", "/.local/share/kwalletd", "/.password-store", "/.pki",
                "/.mozilla", "/.thunderbird", "/.config/chromium", "/.config/google-chrome", "/.config/BraveSoftware",
                "/config/chromium", "/config/google-chrome", "/config/BraveSoftware", "/.var/app", "/.config/rclone",
                "/.config/hub", "/etc/NetworkManager/system-connections", "/etc/sudoers.d",
                // the Assistant's own settings (and wherever they are: own_dir)
                "/.config/a7a-panel"];
    let files = ["/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/ssh/ssh_host_", "/.aws/credentials", "/.netrc",
                 "/.git-credentials", "/.npmrc", "/.pypirc", "/.config/gh/hosts.yml", "/.docker/config.json", "/.kube/config"];
    let suffixes = [".pem", ".key", "_history", ".env"];
    let proc_private = path.starts_with("/proc/") && ["/environ", "/cmdline", "/mem", "/auxv"].iter().any(|s| path.ends_with(s));
    own_dir
        || dirs.iter().any(|d| path.contains(&format!("{d}/")) || path.ends_with(d))
        || files.iter().any(|f| path.contains(f))
        || path.contains("/.ssh/id_")
        || suffixes.iter().any(|s| path.ends_with(s))
        || proc_private
}

pub struct Tools {
    pub taint: Arc<Taint>,
    pub settings: Settings,
    /// where the MCP servers run (the panel's, kept between questions)
    pub pool: Arc<mcp::Pool>,
    /// the switched-on servers' tools, found as a question starts (run)
    pub mcp_tools: Vec<McpTool>,
}

impl Tools {
    pub fn new(taint: Arc<Taint>, settings: Settings, pool: Arc<mcp::Pool>) -> Tools {
        Tools { taint, settings, pool, mcp_tools: Vec::new() }
    }
}

/// A local tool's refusal before it touched anything: it says nothing about the board the model did not already
/// know, so it does not count as a look at local data.
struct Refused(String);

impl Tools {
    /// The real, existing path behind `path`, unless it is a secret. Only an existing absolute path goes on to a
    /// program: a path the model makes up (or one like "-s") never reaches grep's command line.
    fn guard(&self, path: &str) -> Result<PathBuf, Refused> {
        // a folders setting that cannot be used: nothing is looked at, not even whether the path exists
        if !self.settings.folders_error.is_empty() {
            return Err(Refused(format!("the file tools are off: {} (Settings, Save, to set the folders again)", self.settings.folders_error)));
        }
        let home = std::env::var("HOME").unwrap_or_default();
        let p = match path.strip_prefix("~/") {
            Some(rest) => PathBuf::from(&home).join(rest),
            None if path == "~" => PathBuf::from(&home),
            None => PathBuf::from(path),
        };
        if secret(&p.to_string_lossy()) {
            return Err(Refused(format!("{path} looks like a credential file: not read")));
        }
        // judged by what the path really is: a symlink in an innocent place must not lead to a key
        let real = std::fs::canonicalize(&p).map_err(|_| Refused(format!("{path}: no such file or directory")))?;
        if secret(&real.to_string_lossy()) {
            return Err(Refused(format!("{path} looks like a credential file: not read")));
        }
        // the owner's folders, when the settings name any: judged by what both really are (a symlink out of one is
        // out), whole names only ("Documents2" is not in "Documents")
        if !self.settings.folders.is_empty() {
            let inside = self.settings.folders.iter().filter_map(|f| {
                let f = match f.strip_prefix('~') { Some(rest) => format!("{home}{rest}"), None => f.clone() };
                std::fs::canonicalize(f).ok()
            });
            if !inside.into_iter().any(|f| real.starts_with(&f)) {
                return Err(Refused(format!("{path} is outside the folders the owner lets the Assistant read ({})", self.settings.folders.join(", "))));
            }
        }
        Ok(real)
    }

    fn list_dir(&self, path: &str, p: &Path) -> Result<String, String> {
        let mut rows: Vec<String> = std::fs::read_dir(p)
            .map_err(|e| format!("{path}: {e}"))?
            .flatten()
            .map(|e| {
                let size = match e.metadata() { Ok(m) if m.is_dir() => "<dir>".to_string(), Ok(m) => m.len().to_string(), Err(_) => "?".into() };
                format!("{size:>10}  {}", e.file_name().to_string_lossy())
            })
            .collect();
        let total = rows.len();
        rows.sort();
        rows.truncate(200);
        Ok(format!("{} ({total} entries{})\n{}", p.display(), if total > 200 { ", the first 200" } else { "" }, rows.join("\n")))
    }

    fn read_file(&self, path: &str, p: &Path) -> Result<String, String> {
        // a regular file only: a FIFO would wait forever, a device or a directory is no text to read
        let meta = std::fs::metadata(p).map_err(|e| format!("{path}: {e}"))?;
        if !meta.is_file() {
            return Err(format!("{path} is not a regular file"));
        }
        let mut f = std::fs::File::open(p).map_err(|e| format!("{path}: {e}"))?;
        let mut buf = Vec::new();
        // procfs and sysfs files answer in pieces: read until the cap or the end
        f.by_ref().take(MAX_READ as u64 + 1).read_to_end(&mut buf).map_err(|e| format!("{path}: {e}"))?;
        let more = buf.len() > MAX_READ;
        buf.truncate(MAX_READ);
        if buf.contains(&0) {
            return Ok(format!("{} is binary ({} bytes) - not shown", p.display(), meta.len()));
        }
        // lossy: the cap may cut a character in two, which is no reason to call a text binary
        Ok(format!("{} ({} bytes)\n{}{}", p.display(), meta.len(), String::from_utf8_lossy(&buf), if more { "\n...(truncated)" } else { "" }))
    }

    fn search_files(&self, pattern: &str, p: &Path) -> Result<String, String> {
        let out = Command::new("timeout")
            .args(["45", "grep", "-rInZ", "--binary-files=without-match", "-m", "3", "-e", pattern, "--"])
            .arg(p)
            .output()
            .map_err(|e| e.to_string())?;
        let s = String::from_utf8_lossy(&out.stdout);
        // a hit inside a secret file is still a secret (grep prints the file's path as given, absolute here, and a NUL
        // after it: a ':' in a folder's name must not cut the path short, review 2026-11-19)
        let hits: Vec<String> = s.lines().filter_map(|l| l.split_once('\0')).filter(|(path, _)| !secret(path))
            .map(|(path, rest)| format!("{path}:{rest}")).take(20).collect();
        Ok(if hits.is_empty() { format!("no matches for {pattern:?} under {}", p.display()) } else { hits.join("\n") })
    }

    fn system_info(&self, topic: &str) -> Result<String, String> {
        let cmd = match topic.trim().to_lowercase().as_str() {
            "cpu" => "lscpu | head -20",
            "mem" => "free -h; echo; echo 'the programs using the most memory (RSS in MB):'; ps -eo rss=,comm= --sort=-rss | head -8 | awk '{printf \"  %6d MB  %s\\n\", $1/1024, $2}'",
            "disk" => "df -h /",
            "temp" => return Ok(temperatures()),
            "procs" => "echo 'by CPU:'; ps -eo pid,pcpu,pmem,comm --sort=-pcpu | head -8; echo; echo 'by memory:'; ps -eo pid,pmem,rss,comm --sort=-rss | head -8",
            "net" => "ip -brief addr",
            "os" => "head -4 /etc/os-release; uname -a",
            t => return Err(format!("unknown topic {t:?}; use: cpu, mem, disk, temp, procs, net, os")),
        };
        let out = Command::new("bash").args(["-c", cmd]).output().map_err(|e| e.to_string())?;
        Ok(String::from_utf8_lossy(if out.stdout.is_empty() { &out.stderr } else { &out.stdout }).trim().to_string())
    }

    fn web_search(&self, query: &str) -> Result<String, String> {
        // the search engine is a SearXNG on this board (as on the Debian image): a local address on purpose
        let url = format!("{}/search?format=json&language=en&q={}", self.settings.searxng.trim_end_matches('/'), urlencode(query));
        let body = ureq::get(&url).set("User-Agent", UA).timeout(Duration::from_secs(40)).call()
            .map_err(|e| format!("web search is not available ({e}); it needs a SearXNG at {}", self.settings.searxng))?
            .into_string().map_err(|e| e.to_string())?;
        let d: Value = serde_json::from_str(&body).map_err(|e| e.to_string())?;
        let res = d["results"].as_array().cloned().unwrap_or_default();
        if res.is_empty() {
            return Ok(format!("no results for {query:?}"));
        }
        let mut out = vec![format!("{} results for {query:?}:", res.len())];
        for x in res.iter().take(8) {
            let c: String = x["content"].as_str().unwrap_or("").chars().take(220).collect();
            out.push(format!("- {}\n  {}\n  {c}", x["title"].as_str().unwrap_or(""), x["url"].as_str().unwrap_or("")));
        }
        Ok(out.join("\n"))
    }

    fn fetch_url(&self, url: &str) -> Result<String, String> {
        let (body, ctype) = web_get(url, UA, "text/html,application/xhtml+xml,text/plain;q=0.9")?;
        let mut text = body;
        if ctype.contains("html") || text.get(..2000).unwrap_or(&text).to_lowercase().contains("<html") {
            text = html_to_text(&text);
        }
        Ok(text.chars().take(MAX_WEB).collect())
    }

    fn weather(&self, location: &str) -> Result<String, String> {
        // wttr.in answers a browser with an HTML page and curl with the one line asked for (as the Python panel did)
        let url = format!("https://wttr.in/{}?format=%l:+%C+%t+(feels+%f)+humidity+%h+wind+%w", urlencode(location.trim()));
        Ok(web_get(&url, "curl/8.5.0", "text/plain")?.0.trim().to_string())
    }

    /// An MCP server's tool: the one-source rule by what its server reaches (prefs::Reach), then the call.
    fn call_mcp(&self, t: &McpTool, args: &Value) -> (String, String) {
        let shown: String = args.to_string().chars().take(80).collect();
        let audit = format!("{} {shown}", t.fname);
        match t.server.reach {
            prefs::Reach::Web => {
                if !self.settings.web {
                    return ("web access is turned off".into(), format!("BLOCKED {}", t.fname));
                }
                if self.taint.local.load(Ordering::SeqCst) {
                    return (format!("{} reaches the internet, and this chat has looked at this board: it is paused, so nothing local \
                                     can leave through it. Tell the user; a New chat turns it on again.", t.fname),
                            format!("BLOCKED {audit} (this chat has local data)"));
                }
                self.taint.web.store(true, Ordering::SeqCst);
            }
            prefs::Reach::Board => {
                if self.taint.web.load(Ordering::SeqCst) {
                    return (format!("{} reads this board, and this chat has read from the web: it is paused (a page could have asked \
                                     you to read something here and pass it on). Tell the user; a New chat turns it on again.", t.fname),
                            format!("BLOCKED {audit} (this chat has been on the web)"));
                }
                self.taint.local.store(true, Ordering::SeqCst);
            }
            prefs::Reach::Both => {
                // a chat to itself: nothing else before it, nothing else after it (both marks set), only itself again
                if !self.settings.web {
                    return ("web access is turned off".into(), format!("BLOCKED {}", t.fname));
                }
                // bound by the server as the pool knows it: another program saved under its name mid-chat is another
                let me = mcp::key(&t.server);
                let mut bound = self.taint.both.lock().unwrap();
                let mine = bound.as_deref() == Some(me.as_str());
                if !mine && (bound.is_some() || self.taint.local.load(Ordering::SeqCst) || self.taint.web.load(Ordering::SeqCst)) {
                    return (format!("{} both reads this board and reaches the internet, so it works only in a chat that has used no \
                                     other tool. Tell the user; a New chat makes room for it.", t.fname),
                            format!("BLOCKED {audit} (this chat has used other tools)"));
                }
                *bound = Some(me);
                self.taint.local.store(true, Ordering::SeqCst);
                self.taint.web.store(true, Ordering::SeqCst);
            }
        }
        let r = self.pool.get(&t.server, mcp::START).and_then(|c| c.lock().unwrap().call(&t.tool.name, args));
        match r {
            Ok(obs) => {
                let obs = if obs.chars().count() > MAX_OBS { format!("{}\n...(truncated)", obs.chars().take(MAX_OBS).collect::<String>()) } else { obs };
                (obs, audit)
            }
            Err(e) => (format!("error: {e}"), format!("{audit} -> {e}")),
        }
    }

    /// One tool call: (observation for the model, one line for the transcript).
    pub fn call(&self, name: &str, args: &Value) -> (String, String) {
        if let Some(t) = self.mcp_tools.iter().find(|t| t.fname == name) {
            return self.call_mcp(t, args);
        }
        let s = |k: &str| args.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let audit = match name {
            "search_files" => format!("search_files {:?} in {}", s("pattern"), s("path")),
            "web_search" => format!("web_search {:?}", s("query")),
            _ => format!("{name} {}", ["topic", "path", "url", "location"].iter().map(|k| s(k)).find(|v| !v.is_empty()).unwrap_or_default()),
        };
        if WEB_TOOLS.contains(&name) {
            let switched_on = match name {
                "web_search" => self.settings.use_search,
                "fetch_url" => self.settings.use_fetch,
                _ => self.settings.use_weather,
            };
            if !self.settings.web || !switched_on {
                return (format!("{name} is turned off"), format!("BLOCKED {name}"));
            }
            if self.taint.local.load(Ordering::SeqCst) {
                return ("web access is paused in this chat because it has looked at this board, so nothing local can leave \
                         through a web address or a search. Tell the user; a New chat turns the web tools on again."
                            .into(),
                        format!("BLOCKED {audit} (this chat has local data)"));
            }
            // counted before the request: whatever comes back, even an error page, came from outside
            self.taint.web.store(true, Ordering::SeqCst);
        } else if LOCAL_TOOLS.contains(&name) && self.taint.web.load(Ordering::SeqCst) {
            return ("the tools for this board are paused in this chat because it has read from the web: a page could have \
                     asked you to read something here and pass it on. Tell the user; a New chat turns them on again."
                        .into(),
                    format!("BLOCKED {audit} (this chat has been on the web)"));
        }
        let r: Result<String, String> = match name {
            "system_info" => {
                self.taint.local.store(true, Ordering::SeqCst);
                self.system_info(&s("topic"))
            }
            "list_dir" | "read_file" | "search_files" => {
                let path = match (name, s("path")) {
                    ("search_files", p) if p.is_empty() => "~".to_string(),
                    ("list_dir", p) if p.is_empty() => "~".to_string(),
                    (_, p) => p,
                };
                match self.guard(&path) {
                    Err(Refused(e)) => Err(e),
                    Ok(real) => {
                        // past the guard every outcome tells something about the board, an error message too
                        self.taint.local.store(true, Ordering::SeqCst);
                        match name {
                            "list_dir" => self.list_dir(&path, &real),
                            "read_file" => self.read_file(&path, &real),
                            _ => self.search_files(&s("pattern"), &real),
                        }
                    }
                }
            }
            "web_search" => self.web_search(&s("query")),
            "fetch_url" => self.fetch_url(&s("url")),
            "weather" => self.weather(&s("location")),
            _ => Err(format!("unknown tool {name}")),
        };
        match r {
            Ok(obs) => {
                let obs = if obs.chars().count() > MAX_OBS { format!("{}\n...(truncated)", obs.chars().take(MAX_OBS).collect::<String>()) } else { obs };
                (obs, audit)
            }
            Err(e) => (format!("error: {e}"), format!("{audit} -> {e}")),
        }
    }
}

/// The board's temperature sensors under the names a person uses, each with the temperature its zone starts slowing
/// down at (the kernel's zone names left the model guessing which CPU sensor was which, 2026-11-17).
pub fn temperatures() -> String {
    let mut rows = Vec::new();
    let mut zones: Vec<_> = std::fs::read_dir("/sys/class/thermal").map(|d| d.flatten().map(|e| e.path()).collect()).unwrap_or_default();
    zones.sort();
    for z in zones.iter().filter(|z| z.file_name().is_some_and(|n| n.to_string_lossy().starts_with("thermal_zone"))) {
        let kind = std::fs::read_to_string(z.join("type")).unwrap_or_default().trim().to_string();
        let Some(t) = std::fs::read_to_string(z.join("temp")).ok().and_then(|t| t.trim().parse::<i64>().ok()) else { continue };
        let name = match kind.as_str() {
            "cpub_thermal_zone" => "big CPU cores (2x Cortex-A76)".to_string(),
            "cpul_thermal_zone" => "little CPU cores (6x Cortex-A55)".to_string(),
            "cpub_idle_zone" => "big CPU cores, idle-injection sensor".to_string(),
            "cpul_idle_zone" => "little CPU cores, idle-injection sensor".to_string(),
            "gpu_thermal_zone" => "GPU".to_string(),
            "ddr_thermal_zone" => "memory (LPDDR5)".to_string(),
            "npu_thermal_zone" => "NPU".to_string(),
            "skin_zone" => "board surface (skin)".to_string(),
            k => k.trim_end_matches("_thermal_zone").replace('_', " "),
        };
        // the lowest "passive" trip: where the kernel starts to slow the part down
        let slow = (0..8)
            .filter(|i| std::fs::read_to_string(z.join(format!("trip_point_{i}_type"))).is_ok_and(|k| k.trim() == "passive"))
            .filter_map(|i| std::fs::read_to_string(z.join(format!("trip_point_{i}_temp"))).ok()?.trim().parse::<i64>().ok())
            .min();
        rows.push(match slow {
            Some(p) => format!("{name}: {} C (slows down from {} C)", t / 1000, p / 1000),
            None => format!("{name}: {} C", t / 1000),
        });
    }
    if rows.is_empty() {
        return "no temperature sensors found".into();
    }
    format!("{}\n(all the sensors this board has)", rows.join("\n"))
}

// ------------------------------------------------------------------ the web, public addresses only

/// The addresses of `host`, refused unless every one is a public internet address outside this board's own
/// networks. The message never names the address: the model must not learn a local host's address from it.
pub fn public_addresses(host: &str, port: u16) -> Result<Vec<SocketAddr>, String> {
    let addrs: Vec<SocketAddr> = (host, port).to_socket_addrs().map_err(|_| format!("{host}: no such host"))?.collect();
    if addrs.is_empty() {
        return Err(format!("{host}: no such host"));
    }
    let local = local_networks();
    for a in &addrs {
        if !is_public(a.ip()) || local.iter().any(|n| n.contains(a.ip())) {
            return Err(format!("{host} is not a public internet address"));
        }
    }
    Ok(addrs)
}

/// A network this board is on: an address and a prefix length.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Net {
    pub addr: IpAddr,
    pub prefix: u8,
}

impl Net {
    pub fn contains(&self, ip: IpAddr) -> bool {
        match (self.addr, ip) {
            (IpAddr::V4(n), IpAddr::V4(a)) => {
                let m = if self.prefix == 0 { 0 } else { u32::MAX << (32 - self.prefix.min(32) as u32) };
                u32::from(n) & m == u32::from(a) & m
            }
            (IpAddr::V6(n), IpAddr::V6(a)) => {
                let m = if self.prefix == 0 { 0 } else { u128::MAX << (128 - self.prefix.min(128) as u32) };
                u128::from(n) & m == u128::from(a) & m
            }
            _ => false,
        }
    }
}

/// This board's networks: every IPv6 prefix it has an address in (global ones included: a LAN's IPv6 addresses are
/// public addresses, and is_public cannot tell them from the internet) and every IPv4 subnet it reaches directly.
pub fn local_networks() -> Vec<Net> {
    let mut nets = Vec::new();
    // /proc/net/if_inet6: address (32 hex), interface index, prefix length (hex), scope, flags, name
    for l in std::fs::read_to_string("/proc/net/if_inet6").unwrap_or_default().lines() {
        let f: Vec<&str> = l.split_whitespace().collect();
        if let (Some(a), Some(p)) = (f.first(), f.get(2)) {
            if let (Ok(a), Ok(p)) = (u128::from_str_radix(a, 16), u8::from_str_radix(p, 16)) {
                nets.push(Net { addr: IpAddr::V6(std::net::Ipv6Addr::from(a)), prefix: p });
            }
        }
    }
    // /proc/net/route: on-link routes (no gateway), destination and mask in hex, in the kernel's byte order
    for l in std::fs::read_to_string("/proc/net/route").unwrap_or_default().lines().skip(1) {
        let f: Vec<&str> = l.split_whitespace().collect();
        if f.len() < 8 || f[2] != "00000000" {
            continue;
        }
        if let (Ok(d), Ok(m)) = (u32::from_str_radix(f[1], 16), u32::from_str_radix(f[7], 16)) {
            // the kernel prints the network-order word as a host-order number: its bytes, as they lie, are the address
            let (d, m) = (u32::from_be_bytes(d.to_ne_bytes()), u32::from_be_bytes(m.to_ne_bytes()));
            if m != 0 {
                nets.push(Net { addr: IpAddr::V4(std::net::Ipv4Addr::from(d)), prefix: m.count_ones() as u8 });
            }
        }
    }
    nets
}

pub fn is_public(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v) => {
            let o = v.octets();
            !(v.is_private() || v.is_loopback() || v.is_link_local() || v.is_multicast() || v.is_broadcast()
                || v.is_unspecified() || v.is_documentation()
                || o[0] == 0
                || (o[0] == 100 && (o[1] & 0xc0) == 64)            // 100.64.0.0/10: carrier NAT, Tailscale
                || (o[0] == 192 && o[1] == 0 && o[2] == 0)          // 192.0.0.0/24
                || (o[0] == 198 && (o[1] & 0xfe) == 18)             // 198.18.0.0/15: benchmarking
                || o[0] >= 240)                                     // reserved
        }
        IpAddr::V6(v) => {
            if let Some(m) = v.to_ipv4_mapped() {
                return is_public(IpAddr::V4(m));
            }
            let s = v.segments();
            !(v.is_loopback() || v.is_unspecified() || v.is_multicast()
                || (s[0] & 0xfe00) == 0xfc00                        // unique local
                || (s[0] & 0xffc0) == 0xfe80                        // link local
                || (s[0] == 0x2001 && s[1] == 0x0db8)               // documentation
                || (s[0] == 0x64 && s[1] == 0xff9b))                // NAT64 (could reach IPv4 private space)
        }
    }
}

/// The agent of the web tools: one lookup per connection, through the check (ureq connects to what the resolver
/// returns and keeps the name for the certificate), and no automatic redirects.
fn web_agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .resolver(|addr: &str| -> std::io::Result<Vec<SocketAddr>> {
            let (host, port) = addr.rsplit_once(':').ok_or_else(|| std::io::Error::other("no port"))?;
            let host = host.trim_start_matches('[').trim_end_matches(']');
            public_addresses(host, port.parse().map_err(std::io::Error::other)?).map_err(std::io::Error::other)
        })
        .redirects(0)
        .timeout(Duration::from_secs(45))
        .user_agent(UA)
        .build()
}

/// GET a public http(s) URL, following up to five redirects, each one judged again. Returns (body, content type).
pub fn web_get(url: &str, ua: &str, accept: &str) -> Result<(String, String), String> {
    let agent = web_agent();
    let mut url = url.to_string();
    for _ in 0..6 {
        if !(url.starts_with("http://") || url.starts_with("https://")) {
            return Err(format!("refusing non-http url: {url}"));
        }
        let resp = match agent.get(&url).set("User-Agent", ua).set("Accept", accept).call() {
            Ok(r) => r,
            Err(ureq::Error::Status(code, r)) if (300..400).contains(&code) => r,
            Err(ureq::Error::Status(code, _)) => return Err(format!("HTTP {code}")),
            Err(e) => return Err(e.to_string()),
        };
        if (300..400).contains(&resp.status()) {
            let loc = resp.header("Location").ok_or("a redirect without a Location")?.to_string();
            url = if loc.starts_with("http") { loc } else { join_url(&url, &loc) };
            continue;
        }
        let ctype = resp.header("Content-Type").unwrap_or("").to_lowercase();
        let mut raw = Vec::new();
        resp.into_reader().take(2_000_000).read_to_end(&mut raw).map_err(|e| e.to_string())?;
        return Ok((String::from_utf8_lossy(&raw).into_owned(), ctype));
    }
    Err("too many redirects".into())
}

/// A redirect's relative Location against the URL it came from.
pub fn join_url(base: &str, loc: &str) -> String {
    let scheme_end = base.find("://").map(|i| i + 3).unwrap_or(0);
    let host_end = base[scheme_end..].find(['/', '?', '#']).map(|i| scheme_end + i).unwrap_or(base.len());
    if loc.starts_with("//") {
        return format!("{}{loc}", &base[..scheme_end - 2]);
    }
    if loc.starts_with('/') {
        return format!("{}{loc}", &base[..host_end]);
    }
    // relative to the directory of the path (the host itself when there is no path)
    let path = &base[host_end..];
    let path = &path[..path.find(['?', '#']).unwrap_or(path.len())];
    let dir = path.rfind('/').map(|i| &path[..=i]).unwrap_or("/");
    format!("{}{dir}{loc}", &base[..host_end])
}

pub fn urlencode(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => (b as char).to_string(),
            b' ' => "+".into(),
            _ => format!("%{b:02X}"),
        })
        .collect()
}

/// Readable text of an HTML page: head, scripts, styles and navigation dropped before the tags are.
pub fn html_to_text(html: &str) -> String {
    use regex::Regex;
    let title = Regex::new(r"(?is)<title[^>]*>(.*?)</title>").unwrap().captures(html).map(|c| c[1].trim().to_string());
    let mut t = Regex::new(r"(?is)<head\b.*?</head>").unwrap().replace_all(html, " ").into_owned();
    for tag in ["script", "style", "noscript", "svg", "template", "nav", "footer", "form"] {
        t = Regex::new(&format!(r"(?is)<{tag}\b.*?</{tag}>")).unwrap().replace_all(&t, " ").into_owned();
    }
    t = Regex::new(r"(?is)<!--.*?-->").unwrap().replace_all(&t, " ").into_owned();
    t = Regex::new(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>").unwrap().replace_all(&t, "\n").into_owned();
    t = Regex::new(r"(?s)<[^>]*>").unwrap().replace_all(&t, " ").into_owned();
    for (a, b) in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", "\""), ("&#39;", "'"), ("&nbsp;", " ")] {
        t = t.replace(a, b);
    }
    t = Regex::new(r"[ \t\r\x0c\x0b]+").unwrap().replace_all(&t, " ").into_owned();
    t = Regex::new(r"\n\s*\n\s*").unwrap().replace_all(&t, "\n\n").into_owned();
    let body: Vec<&str> = t.lines().map(str::trim).filter(|l| l.chars().count() > 2 || l.is_empty()).collect();
    match title {
        Some(ti) => format!("{ti}\n\n{}", body.join("\n")),
        None => body.join("\n"),
    }
}

// ------------------------------------------------------------------ one question

/// Streams one completion. Returns (text, tok/s, tokens, tool calls as (name, raw arguments)).
fn stream(messages: &[Value], settings: &Settings, tools: &[Value], stop: &AtomicBool, out: &dyn Fn(Event)) -> Result<(String, f32, u32, Vec<(String, String)>), String> {
    let mut payload = json!({"model": "local", "messages": messages, "max_tokens": settings.max_tokens,
                             "temperature": settings.temperature, "stream": true});
    if settings.tools && !tools.is_empty() {
        payload["tools"] = Value::Array(tools.to_vec());
    }
    let resp = ureq::post(&format!("{}/v1/chat/completions", api()))
        .set("Content-Type", "application/json")
        .timeout(Duration::from_secs(1200))
        .send_string(&payload.to_string())
        .map_err(|e| match e {
            ureq::Error::Status(503, _) => "the model is still loading: wait for the status to turn green, then send again".to_string(),
            ureq::Error::Status(c, r) => {
                // llama-server says why in the body: {"error": {"message": ..., "type": ...}}
                let body = r.into_string().unwrap_or_default();
                let why: Value = serde_json::from_str(&body).unwrap_or(Value::Null);
                let kind = why["error"]["type"].as_str().unwrap_or("");
                let msg = why["error"]["message"].as_str().unwrap_or(&body).chars().take(300).collect::<String>();
                if kind.contains("context") || msg.contains("context") {
                    "this chat no longer fits into the model's memory (its context): press New chat, or ask for less at once".to_string()
                } else {
                    format!("the server answered HTTP {c}: {msg}")
                }
            }
            e => format!("cannot reach the model server (press Start): {e}"),
        })?;
    let (mut text, mut tok_s, mut n) = (String::new(), 0.0f32, 0u32);
    let mut calls: Vec<(String, String)> = Vec::new();
    for line in BufReader::new(resp.into_reader()).lines() {
        if stop.load(Ordering::SeqCst) {
            break;
        }
        let line = line.map_err(|e| e.to_string())?;
        let Some(body) = line.trim().strip_prefix("data:") else { continue };
        let body = body.trim();
        if body == "[DONE]" {
            break;
        }
        let Ok(d) = serde_json::from_str::<Value>(body) else { continue };
        let delta = &d["choices"][0]["delta"];
        if let Some(piece) = delta["content"].as_str() {
            if !piece.is_empty() {
                text.push_str(piece);
                if !text.contains("<tool_call>") {
                    out(Event::Chunk(piece.to_string()));
                }
            }
        }
        if let Some(tcs) = delta["tool_calls"].as_array() {
            for tc in tcs {
                let i = tc["index"].as_u64().unwrap_or(0) as usize;
                while calls.len() <= i {
                    calls.push((String::new(), String::new()));
                }
                if let Some(name) = tc["function"]["name"].as_str() {
                    calls[i].0 = name.to_string();
                }
                if let Some(a) = tc["function"]["arguments"].as_str() {
                    calls[i].1.push_str(a);
                }
            }
        }
        if let Some(t) = d.get("timings") {
            tok_s = t["predicted_per_second"].as_f64().map(|v| v as f32).unwrap_or(tok_s);
            n = t["predicted_n"].as_u64().map(|v| v as u32).unwrap_or(n);
        }
    }
    calls.retain(|c| !c.0.is_empty());
    Ok((text, tok_s, n, calls))
}

/// Does the board's SearXNG answer? (asked once per question: without it web_search is not offered at all)
fn searxng_up(url: &str) -> bool {
    ureq::get(url).timeout(Duration::from_millis(700)).call().is_ok()
}

/// Past `budget` characters, the oldest tool results of this question are cut short: the newest ones matter most, and
/// the server refuses a request that does not fit its context.
fn fit(msgs: &mut [Value], budget: usize) {
    let size = |m: &[Value]| m.iter().map(|x| x["content"].as_str().map_or(0, str::len)).sum::<usize>();
    for i in 0..msgs.len() {
        if size(msgs) <= budget {
            break;
        }
        if msgs[i]["role"] == "tool" {
            let c = msgs[i]["content"].as_str().unwrap_or("");
            if c.len() > 400 {
                let short: String = c.chars().take(300).collect();
                msgs[i]["content"] = Value::String(format!("{short}\n...(shortened: the chat ran long)"));
            }
        }
    }
}

/// One question: up to six rounds of tool calls, then the answer. Runs on a thread of its own; `out` reaches the UI.
pub fn run(history: Vec<Value>, tools: Tools, stop: Arc<AtomicBool>, out: impl Fn(Event)) {
    let mut tools = tools;
    if tools.settings.web && tools.settings.use_search {
        tools.settings.search = searxng_up(&tools.settings.searxng);
    }
    // the switched-on MCP servers' tools, each started on a thread of its own when it is not running yet (a slow one
    // does not hold up the others; one that does not start is said, and the question goes on without it); those
    // that reach the internet not while the web tools are off
    let wanted: Vec<prefs::Server> = tools.settings.mcp.iter().filter(|s| tools.settings.web || s.reach == prefs::Reach::Board).cloned().collect();
    let (tx, rx) = std::sync::mpsc::channel();
    for server in wanted.clone() {
        if tools.pool.would_start(&server) {
            out(Event::Note(format!("starting the MCP server {}…", server.name)));
        }
        let (pool, tx) = (tools.pool.clone(), tx.clone());
        std::thread::spawn(move || {
            let r = pool.get(&server, mcp::START).map(|c| c.lock().unwrap().tools.clone());
            let _ = tx.send((server, r));
        });
    }
    drop(tx);
    let mut started = Vec::new();
    loop {
        if stop.load(Ordering::SeqCst) {
            return; // (the tab has gone on: what still starts stays in the pool for the next question)
        }
        match rx.recv_timeout(Duration::from_millis(200)) {
            Ok(r) => started.push(r),
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }
    started.sort_by_key(|(s, _)| wanted.iter().position(|w| w.name == s.name)); // the settings' order
    for (server, r) in started {
        match r {
            Ok(found) => {
                for t in found.iter().filter(|t| !server.off_tools.contains(&t.name)).map(|t| McpTool::new(&server, t)) {
                    // a name the model already has (a server "web" with a tool "search" is web_search) would take that
                    // tool's place, and its place in the one-source rule: left out, and said
                    if LOCAL_TOOLS.contains(&t.fname.as_str()) || WEB_TOOLS.contains(&t.fname.as_str()) || tools.mcp_tools.iter().any(|m| m.fname == t.fname) {
                        out(Event::Note(format!("MCP server {}: its tool {} would be called {}, a name already taken: left out", server.name, t.tool.name, t.fname)));
                        continue;
                    }
                    tools.mcp_tools.push(t);
                }
            }
            Err(e) => out(Event::Note(format!("MCP server {}: {e}", server.name))),
        }
    }
    let mut msgs = history;
    // the system prompt as the settings are now (the tab keeps a placeholder in its history)
    if msgs.first().is_some_and(|m| m["role"] == "system") {
        msgs[0] = json!({"role": "system", "content": system_prompt(&tools)});
    }
    let offered = schemas(&tools.settings, &tools.mcp_tools);
    let context = running_context(&tools.settings);
    let room = budget(&tools.settings, context, offered.iter().map(|t| t.to_string().len()).sum());
    if room < 1500 {
        // the question itself would not fit: said as it is, not as a chat that ran long
        return out(Event::Fail(format!(
            "the tools' descriptions and an answer of {} tokens leave no room in the model's context ({context} tokens): switch some \
             tools or MCP servers off, choose shorter answers, or a larger context (Settings; then Stop server and Start)",
            tools.settings.max_tokens)));
    }
    let (mut turn, mut tokens, mut rate) = (String::new(), 0u32, 0.0f32);
    for round in 0..=MAX_TOOL_ROUNDS {
        if stop.load(Ordering::SeqCst) {
            break;
        }
        fit(&mut msgs, room);
        // the last round offers no tools: the model has to answer with what it has
        let mut settings = tools.settings.clone();
        settings.tools &= round < MAX_TOOL_ROUNDS;
        let (text, r, n, calls) = match stream(&msgs, &settings, &offered, &stop, &out) {
            Ok(x) => x,
            Err(e) => return out(Event::Fail(e)),
        };
        tokens += n;
        if r > 0.0 {
            rate = r;
        }
        turn.push_str(&text);
        if calls.is_empty() && text.contains("<tool_call>") {
            // a call written out as text that the server did not take as one: say so rather than show nothing
            let note = "\n\n(the model asked for a tool in a form the server did not understand)";
            out(Event::Chunk(note.into()));
            turn.push_str(note);
        }
        if stop.load(Ordering::SeqCst) || calls.is_empty() {
            break;
        }
        // the assistant's calls echoed back, then one result per call: the chat template needs that pairing
        msgs.push(json!({"role": "assistant", "content": if text.is_empty() { Value::Null } else { Value::String(text.clone()) },
                         "tool_calls": calls.iter().enumerate().map(|(i, (name, raw))| json!({"id": format!("c{i}"), "type": "function",
                             "function": {"name": name, "arguments": if raw.trim().is_empty() { "{}" } else { raw }}})).collect::<Vec<_>>()}));
        for (i, (name, raw)) in calls.iter().enumerate() {
            if stop.load(Ordering::SeqCst) {
                // stopped: no more tools, and no new request; what was asked is answered with nothing
                return out(Event::Done { text: turn, tok_s: rate, tokens });
            }
            let args: Value = serde_json::from_str(raw).unwrap_or(json!({}));
            let (obs, audit) = tools.call(name, &args);
            out(Event::Tool { audit, observation: obs.clone() });
            msgs.push(json!({"role": "tool", "tool_call_id": format!("c{i}"), "name": name,
                             "content": obs.chars().take(MAX_READ).collect::<String>()}));
        }
    }
    out(Event::Done { text: turn, tok_s: rate, tokens });
}

/// Is the server up and its model loaded? (llama-server: /health answers 200 when ready, 503 while loading)
pub fn health() -> &'static str {
    match ureq::get(&format!("{}/health", api())).timeout(Duration::from_millis(800)).call() {
        Ok(_) => "ready",
        Err(ureq::Error::Status(503, _)) => "loading",
        Err(_) => "stopped",
    }
}

/// The model `a7a-llm get` downloads, and its size (GET_URL and GET_SIZE in tools/board/llm/a7a-llm; a test keeps the
/// two in step): the panel shows the download's progress from the size of the part already on disk.
pub const DEFAULT_MODEL: &str = "Qwen3-4B-Instruct-2507-Q4_0.gguf";
pub const DEFAULT_MODEL_SIZE: u64 = 2_375_773_280;

/// How far `a7a-llm get` is, 0..1, while its part file is on disk.
pub fn download_progress() -> Option<f32> {
    let part = Path::new(&models_dir()).join(format!("{DEFAULT_MODEL}.part"));
    std::fs::metadata(part).ok().map(|m| (m.len() as f64 / DEFAULT_MODEL_SIZE as f64).min(1.0) as f32)
}

/// Where a7a-llm keeps the models: A7A_LLM_MODELS, else ~/models.
pub fn models_dir() -> String {
    std::env::var("A7A_LLM_MODELS").unwrap_or_else(|_| format!("{}/models", std::env::var("HOME").unwrap_or_default()))
}

/// The models on this board, by file name (adapters left out).
pub fn models() -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(Path::new(&models_dir()))
        .map(|d| d.flatten().map(|e| e.file_name().to_string_lossy().into_owned())
             .filter(|n| n.ends_with(".gguf") && !n.contains("lora")).collect())
        .unwrap_or_default();
    v.sort();
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A directory of its own per test, removed afterwards.
    struct Dir(PathBuf);
    impl Dir {
        fn new(name: &str) -> Dir {
            let d = std::env::temp_dir().join(format!("a7a-agent-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&d);
            std::fs::create_dir_all(&d).unwrap();
            Dir(d)
        }
        fn path(&self, rel: &str) -> String {
            self.0.join(rel).to_string_lossy().into_owned()
        }
    }
    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn tools() -> Tools {
        Tools::new(Arc::new(Taint::default()), Settings::default(), Arc::new(mcp::Pool::default()))
    }

    #[test]
    fn local_and_private_addresses_are_not_public() {
        for ip in ["127.0.0.1", "10.1.2.3", "192.168.1.182", "172.20.0.1", "169.254.1.1", "100.101.102.103", "0.0.0.0",
                   "::1", "fe80::1", "fd00::5", "::ffff:192.168.1.1", "64:ff9b::a00:1", "255.255.255.255"] {
            assert!(!is_public(ip.parse().unwrap()), "{ip}");
        }
        for ip in ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"] {
            assert!(is_public(ip.parse().unwrap()), "{ip}");
        }
    }

    #[test]
    fn this_boards_own_networks_are_refused_ipv6_included() {
        // a LAN's IPv6 addresses are global ones: only the prefixes this machine is on tell them from the internet
        let net = Net { addr: "2001:db8:12:f901::".parse().unwrap(), prefix: 64 };
        assert!(net.contains("2001:db8:12:f901:1234::1".parse().unwrap()));
        assert!(!net.contains("2001:db8:12:f902::1".parse().unwrap()));
        let net4 = Net { addr: "192.168.1.0".parse().unwrap(), prefix: 24 };
        assert!(net4.contains("192.168.1.163".parse().unwrap()) && !net4.contains("192.168.3.1".parse().unwrap()));
        // and on this machine: every address it has is inside one of its networks, so none of them is fetched
        let local = local_networks();
        for l in std::fs::read_to_string("/proc/net/if_inet6").unwrap_or_default().lines() {
            let a = u128::from_str_radix(l.split_whitespace().next().unwrap(), 16).unwrap();
            let ip = IpAddr::V6(std::net::Ipv6Addr::from(a));
            assert!(local.iter().any(|n| n.contains(ip)), "{ip} is not in {local:?}");
        }
        // and the address this machine sends from is inside one of its IPv4 networks; while it has no route at all
        // (the router restarting, 2026-11-17) there is no such address and nothing to compare
        if let Ok(s) = std::net::UdpSocket::bind("0.0.0.0:0") {
            if s.connect("192.0.2.1:9").is_ok() {
                let ip = s.local_addr().unwrap().ip();
                assert!(local.iter().any(|n| n.contains(ip)), "{ip}, the address this machine sends from, is not in {local:?}");
            }
        }
    }

    #[test]
    fn secrets() {
        for p in ["/home/radxa/.ssh/id_ed25519", "/home/radxa/.ssh", "/home/radxa/.mozilla/firefox/x/cookies.sqlite",
                  "/etc/NetworkManager/system-connections/home.nmconnection", "/home/radxa/.bash_history", "/etc/shadow",
                  "/home/radxa/.git-credentials", "/proc/1234/environ", "/proc/self/cmdline", "/etc/ssh/ssh_host_ed25519_key",
                  "/home/radxa/.var/app/org.chromium.Chromium/config/chromium/Default/Login Data",
                  "/home/radxa/.local/share/kwalletd/kdewallet.kwl"] {
            assert!(secret(p), "{p}");
        }
        for p in ["/home/radxa/notes.txt", "/etc/os-release", "/proc/cpuinfo", "/home/radxa/ssh-notes.md", "/proc/1234/status"] {
            assert!(!secret(p), "{p}");
        }
    }

    #[test]
    fn a_chat_with_local_data_gets_no_web() {
        let d = Dir::new("local-first");
        std::fs::write(d.path("notes.txt"), "hello").unwrap();
        let t = tools();
        let (obs, _) = t.call("read_file", &json!({"path": d.path("notes.txt")}));
        assert!(obs.contains("hello"));
        for (name, args) in [("fetch_url", json!({"url": "https://1.1.1.1/?d=hello"})), ("web_search", json!({"query": "hello"})), ("weather", json!({"location": "hello"}))] {
            let (obs, audit) = t.call(name, &args);
            assert!(obs.contains("paused"), "{name}: {obs}");
            assert!(audit.starts_with("BLOCKED"));
        }
        assert!(!t.taint.web.load(Ordering::SeqCst));
    }

    #[test]
    fn a_chat_that_has_been_on_the_web_gets_no_local_tools() {
        // a page can tell the model to read a file here and put it into a link of its answer
        let d = Dir::new("web-first");
        std::fs::write(d.path("notes.txt"), "hello").unwrap();
        let t = tools();
        let (obs, _) = t.call("fetch_url", &json!({"url": "http://127.0.0.1:9/"})); // refused, but it was a web call
        assert!(obs.starts_with("error:"), "{obs}");
        for (name, args) in [("read_file", json!({"path": d.path("notes.txt")})), ("list_dir", json!({"path": d.path("")})),
                             ("search_files", json!({"pattern": "hello", "path": d.path("")})), ("system_info", json!({"topic": "os"}))] {
            let (obs, audit) = t.call(name, &args);
            assert!(obs.contains("paused") && !obs.contains("hello"), "{name}: {obs}");
            assert!(audit.starts_with("BLOCKED"), "{audit}");
        }
        assert!(!t.taint.local.load(Ordering::SeqCst));
    }

    #[test]
    fn a_failed_local_call_still_counts_but_a_refused_one_does_not() {
        let d = Dir::new("taint");
        std::fs::write(d.path("file"), "x").unwrap();
        // refused before touching anything: a key, a path that is not there
        let t = tools();
        t.call("read_file", &json!({"path": d.path(".ssh/id_x")}));
        t.call("list_dir", &json!({"path": d.path("missing")}));
        assert!(!t.taint.local.load(Ordering::SeqCst));
        // past the guard, an error says something about the board (a file's real name, say): it counts
        let (obs, _) = t.call("list_dir", &json!({"path": d.path("file")}));
        assert!(obs.starts_with("error:"), "{obs}");
        assert!(t.taint.local.load(Ordering::SeqCst));
    }

    #[test]
    fn search_files_takes_no_options_from_the_model() {
        // review 2026-11-17: path "-s" went to grep as an option, grep searched the working directory and printed
        // relative paths (.ssh/id_...) that the secret filter did not recognise
        let d = Dir::new("grep");
        std::fs::create_dir_all(d.path(".ssh")).unwrap();
        std::fs::write(d.path(".ssh/id_ed25519"), "line\nSECRET-KEY\n").unwrap();
        std::fs::write(d.path("notes.txt"), "SECRET-KEY is not here\n").unwrap();
        let t = tools();
        for path in ["-s", "--include=*", "-r"] {
            let (obs, _) = t.call("search_files", &json!({"pattern": "SECRET", "path": path}));
            assert!(!obs.contains("SECRET-KEY"), "{path}: {obs}");
        }
        let (obs, _) = t.call("search_files", &json!({"pattern": "SECRET", "path": d.path("")}));
        assert!(obs.contains("notes.txt") && !obs.contains("id_ed25519"), "{obs}");
    }

    #[test]
    fn a_symlink_to_a_key_is_refused_and_does_not_count_as_local_data() {
        let d = Dir::new("sym");
        std::fs::create_dir_all(d.path(".ssh")).unwrap();
        std::fs::write(d.path(".ssh/id_test"), "PRIVATE").unwrap();
        std::os::unix::fs::symlink(d.path(".ssh/id_test"), d.path("innocent.txt")).unwrap();
        let t = tools();
        let (obs, _) = t.call("read_file", &json!({"path": d.path("innocent.txt")}));
        assert!(obs.starts_with("error:") && !obs.contains("PRIVATE"), "{obs}");
        assert!(!t.taint.local.load(Ordering::SeqCst));
    }

    #[test]
    fn read_file_reads_text_whole_and_refuses_what_is_not_a_file() {
        let d = Dir::new("read");
        // a character across the 3000-byte cap is no reason to call a text binary
        let text = format!("{}°C and more", "a".repeat(MAX_READ - 1));
        std::fs::write(d.path("t.txt"), &text).unwrap();
        let (obs, _) = tools().call("read_file", &json!({"path": d.path("t.txt")}));
        assert!(!obs.contains("binary") && obs.contains("truncated"), "{}", &obs[..120]);
        std::fs::write(d.path("b.bin"), [0u8, 1, 2, 3]).unwrap();
        let (obs, _) = tools().call("read_file", &json!({"path": d.path("b.bin")}));
        assert!(obs.contains("binary"), "{obs}");
        // a FIFO would block a read forever
        assert!(Command::new("mkfifo").arg(d.path("fifo")).status().unwrap().success());
        let (obs, _) = tools().call("read_file", &json!({"path": d.path("fifo")}));
        assert!(obs.contains("not a regular file"), "{obs}");
    }

    #[test]
    fn a_name_is_judged_by_the_address_it_resolves_to() {
        // the check sits in the connection's own lookup: a name that answers with this board is refused, and the
        // message does not tell the model which address that was
        let err = web_get("http://localhost:8080/v1/models", UA, "*/*").unwrap_err();
        assert!(err.contains("not a public internet address") && !err.contains("127.0.0.1"), "{err}");
        assert!(web_get("file:///etc/passwd", UA, "*/*").is_err());
    }

    #[test]
    fn redirects_resolve_against_the_right_base() {
        assert_eq!(join_url("https://a.com", "foo"), "https://a.com/foo");
        assert_eq!(join_url("https://a.com/x/y.html?q=1", "z"), "https://a.com/x/z");
        assert_eq!(join_url("https://a.com/x/y", "/root"), "https://a.com/root");
        assert_eq!(join_url("https://a.com/x", "//b.org/p"), "https://b.org/p");
    }

    #[test]
    fn long_questions_are_cut_to_fit() {
        let mut m = vec![json!({"role": "system", "content": "s"}), json!({"role": "user", "content": "q"})];
        for _ in 0..8 {
            m.push(json!({"role": "tool", "content": "x".repeat(3000)}));
        }
        let room = budget(&Settings::default(), 8192, 0);
        fit(&mut m, room);
        let total: usize = m.iter().map(|x| x["content"].as_str().unwrap().len()).sum();
        assert!(total <= room, "{total}");
        // the newest result stays whole
        assert_eq!(m.last().unwrap()["content"].as_str().unwrap().len(), 3000);
    }

    #[test]
    fn the_budget_follows_the_running_context_and_the_tools() {
        let d = Settings::default();
        assert_eq!(budget(&d, 8192, 2000), 19_040); // about what it was before the setting (20 000 for 8192)
        assert!(budget(&d, 16384, 2000) > 40_000);
        // the server's own context counts, not a newer setting it has not started with
        assert_eq!(budget(&Settings { context: 16384, ..Settings::default() }, 8192, 2000), 19_040);
        // nothing left: no pretend floor (run says so instead of sending a question that cannot fit)
        assert_eq!(budget(&Settings { max_tokens: 2048, ..Settings::default() }, 4096, 9000), 0);
    }

    #[test]
    fn a_server_that_reads_and_reaches_out_gets_a_chat_to_itself() {
        let d = mcp::tests::scratch("both");
        let mut server = mcp::tests::fake_server(&d, "fake-both");
        server.reach = prefs::Reach::Both;
        let mut t = tools();
        let found = t.pool.get(&server, mcp::START).unwrap().lock().unwrap().tools.clone();
        t.mcp_tools = found.iter().map(|x| McpTool::new(&server, x)).collect();
        assert_eq!(t.call("fake-both_echo", &json!({"text": "a"})).0, "you said a\n[image]");
        assert_eq!(t.call("fake-both_echo", &json!({"text": "b"})).0, "you said b\n[image]"); // itself again: on
        // after it, nothing else: what it read must not go out by the web tools, nor meet the board's
        assert!(t.call("fetch_url", &json!({"url": "https://example.org"})).1.starts_with("BLOCKED"));
        assert!(t.call("read_file", &json!({"path": "/etc/hostname"})).1.starts_with("BLOCKED"));
        // and in a chat that has used something else, it does not run
        let mut u = tools();
        u.mcp_tools = t.mcp_tools.clone();
        let dd = Dir::new("both-first");
        std::fs::write(dd.path("f.txt"), "x").unwrap();
        u.call("read_file", &json!({"path": dd.path("f.txt")}));
        let (obs, audit) = u.call("fake-both_echo", &json!({"text": "secret"}));
        assert!(obs.contains("no other tool") && audit.starts_with("BLOCKED"), "{audit}");
        // nor with the web switched off
        let mut w = tools();
        w.mcp_tools = t.mcp_tools.clone();
        w.settings.web = false;
        assert_eq!(w.call("fake-both_echo", &json!({})).1, "BLOCKED fake-both_echo");
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn the_assistants_own_settings_are_a_secret() {
        assert!(secret("/home/radxa/.config/a7a-panel/assistant.json"));
        assert!(secret("/home/radxa/.config/a7a-panel"));
        // where XDG_CONFIG_HOME puts them, and through a link to there
        let d = Dir::new("own-settings");
        std::fs::create_dir_all(d.path("cfg/a7a-panel")).unwrap();
        std::os::unix::fs::symlink(d.path("cfg"), d.path("link")).unwrap();
        let own = PathBuf::from(d.path("link/a7a-panel/assistant.json"));
        for p in [d.path("link/a7a-panel/assistant.json"), d.path("link/a7a-panel/assistant.json.tmp"), d.path("link/a7a-panel"),
                  std::fs::canonicalize(d.path("cfg")).unwrap().join("a7a-panel/assistant.json").to_string_lossy().into_owned()] {
            assert!(secret_beside(&p, &own), "{p}");
        }
        for p in [d.path("link/a7a-panel-notes.txt"), d.path("cfg/notes.txt")] {
            assert!(!secret_beside(&p, &own), "{p}");
        }
    }

    #[test]
    fn folders_that_cannot_be_used_read_nothing() {
        let d = Dir::new("closed");
        std::fs::write(d.path("f.txt"), "x").unwrap();
        let mut t = tools();
        t.settings.folders_error = "none of the folders reads ([\"Documents\"])".into();
        let (obs, _) = t.call("read_file", &json!({"path": d.path("f.txt")}));
        assert!(obs.contains("the file tools are off"), "{obs}");
        assert!(!t.taint.local.load(Ordering::SeqCst)); // refused before it touched anything
    }

    #[test]
    fn a_colon_in_a_folders_name_does_not_hide_a_secret_from_the_filter() {
        // `date -Is` names a backup folder with colons: the path before grep's NUL is the whole path
        let d = Dir::new("colon");
        let keys = d.path("backup-2026-11-19T10:00/.ssh");
        std::fs::create_dir_all(&keys).unwrap();
        std::fs::write(format!("{keys}/id_ed25519"), "b3BlbnNzaC1rZXk\n").unwrap();
        std::fs::write(d.path("notes.txt"), "b3BlbnNzaC1rZXk mentioned here\n").unwrap();
        let (obs, _) = tools().call("search_files", &json!({"pattern": "b3Bl", "path": d.path("")}));
        assert!(!obs.contains("id_ed25519"), "{obs}");
        assert!(obs.contains("notes.txt:1:b3BlbnNzaC1rZXk mentioned here"), "{obs}");
    }

    #[test]
    fn the_owners_folders_are_the_only_ones_read() {
        let d = Dir::new("folders");
        for f in ["Documents/notes.txt", "Documents2/other.txt", "Music/song.txt"] {
            std::fs::create_dir_all(Path::new(&d.path(f)).parent().unwrap()).unwrap();
            std::fs::write(d.path(f), "text").unwrap();
        }
        std::os::unix::fs::symlink(d.path("Music"), d.path("Documents/music")).unwrap();
        let mut t = tools();
        t.settings.folders = vec![d.path("Documents/")];
        assert!(t.call("read_file", &json!({"path": d.path("Documents/notes.txt")})).0.contains("text"));
        for outside in ["Documents2/other.txt", "Music/song.txt", "Documents/music/song.txt", "Documents/../Music/song.txt"] {
            let (obs, _) = t.call("read_file", &json!({"path": d.path(outside)}));
            assert!(obs.contains("outside the folders"), "{outside}: {obs}");
        }
        assert!(t.call("list_dir", &json!({"path": d.path("Documents")})).0.contains("notes.txt"));
        assert!(t.call("search_files", &json!({"pattern": "text", "path": d.path("")})).0.contains("outside the folders"));
        // none named: everywhere but the secrets, as before
        t.settings.folders.clear();
        assert!(t.call("read_file", &json!({"path": d.path("Music/song.txt")})).0.contains("text"));
    }

    #[test]
    fn a_web_tool_switched_off_is_neither_offered_nor_run() {
        let mut s = Settings { use_search: false, use_weather: false, ..Settings::default() };
        let names = |s: &Settings| schemas(s, &[]).iter().map(|t| t["function"]["name"].as_str().unwrap().to_string()).collect::<Vec<_>>();
        assert_eq!(names(&s), ["system_info", "list_dir", "read_file", "search_files", "fetch_url"]);
        s.web = false;
        assert!(!names(&s).contains(&"fetch_url".to_string()));
        let mut t = tools();
        t.settings.use_weather = false;
        let (obs, audit) = t.call("weather", &json!({"location": "Lisbon"}));
        assert_eq!((obs.as_str(), audit.as_str()), ("weather is turned off", "BLOCKED weather"));
        assert!(!t.taint.web.load(Ordering::SeqCst));
    }

    #[test]
    fn the_owners_instructions_and_the_tools_are_in_the_prompt() {
        let mut t = tools();
        t.settings.instructions = "  Answer in two sentences.\n".into();
        t.settings.use_weather = false;
        let p = system_prompt(&t);
        assert!(p.starts_with(SYSTEM_PROMPT));
        assert!(p.contains("system_info, list_dir, read_file, search_files, web_search, fetch_url."), "{p}");
        assert!(p.ends_with("The owner's instructions:\nAnswer in two sentences."), "{p}");
        t.settings.instructions.clear();
        assert!(!system_prompt(&t).contains("instructions"));
    }

    #[test]
    fn an_mcp_servers_tools_are_offered_called_and_kept_to_the_one_source_rule() {
        let d = mcp::tests::scratch("agent");
        let board = mcp::tests::fake_server(&d, "fake-agent");
        let mut t = tools();
        let c = t.pool.get(&board, mcp::START).unwrap();
        let found = c.lock().unwrap().tools.clone();
        t.mcp_tools = found.iter().map(|x| McpTool::new(&board, x)).collect();
        let names: Vec<String> = schemas(&t.settings, &t.mcp_tools).iter().map(|x| x["function"]["name"].as_str().unwrap().to_string()).collect();
        assert!(names.ends_with(&["fake-agent_echo".to_string(), "fake-agent_fail".into(), "fake-agent_slow".into()]), "{names:?}");
        assert!(system_prompt(&t).contains("fake-agent_echo"));
        let (obs, audit) = t.call("fake-agent_echo", &json!({"text": "hi"}));
        assert_eq!(obs, "you said hi\n[image]");
        assert!(audit.starts_with("fake-agent_echo {"), "{audit}");
        assert!(t.taint.local.load(Ordering::SeqCst)); // it reads this board
        assert!(t.call("fake-agent_fail", &json!({})).0.starts_with("error: it broke"));
        // after the board, a server that reaches the internet is paused, as the web tools are
        let mut web = board.clone();
        web.reach = prefs::Reach::Web;
        t.mcp_tools = found.iter().map(|x| McpTool::new(&web, x)).collect();
        let (obs, audit) = t.call("fake-agent_echo", &json!({"text": "secret"}));
        assert!(obs.contains("paused") && audit.starts_with("BLOCKED"), "{audit}");
        // and with the web switched off it is not offered at all, nor run
        t.settings.web = false;
        assert!(schemas(&t.settings, &t.mcp_tools).iter().all(|x| !x["function"]["name"].as_str().unwrap().starts_with("fake-agent")));
        assert_eq!(t.call("fake-agent_echo", &json!({})).1, "BLOCKED fake-agent_echo");
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn an_mcp_tools_name_is_one_the_model_may_call() {
        let server = prefs::Server { name: "files".into(), ..Default::default() };
        let tool = mcp::Tool { name: "read file/v2".into(), description: String::new(), schema: json!({}) };
        assert_eq!(McpTool::new(&server, &tool).fname, "files_read_file_v2");
        let long = mcp::Tool { name: "x".repeat(100), ..tool };
        assert_eq!(McpTool::new(&server, &long).fname.len(), 64);
    }

    #[test]
    fn the_default_model_is_the_one_a7a_llm_downloads() {
        let script = include_str!("../../board/llm/a7a-llm");
        let value = |key: &str| script.lines().find_map(|l| l.strip_prefix(&format!("{key}="))).unwrap_or_default().to_string();
        assert!(value("GET_URL").ends_with(&format!("/{DEFAULT_MODEL}")), "{}", value("GET_URL"));
        assert_eq!(value("GET_SIZE"), DEFAULT_MODEL_SIZE.to_string());
    }

    #[test]
    fn html_becomes_text() {
        let t = html_to_text("<html><head><title>T</title><style>x{}</style></head><body><nav>menu</nav><p>Hello there</p><script>bad()</script></body></html>");
        assert!(t.starts_with("T\n"));
        assert!(t.contains("Hello there"));
        assert!(!t.contains("bad()") && !t.contains("menu"));
    }
}
