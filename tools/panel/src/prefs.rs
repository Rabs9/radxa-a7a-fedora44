//! The Assistant's settings, the owner's: instructions added to its prompt, the folders its file tools may read, which
//! web tools it has, MCP servers (programs that give it more tools), and how much it remembers and answers. One file,
//! ~/.config/a7a-panel/assistant.json, written by the Assistant tab's Settings and read by the tab, by `a7a-panel
//! --answer` (the Assistant by voice) and by `a7a-llm serve` (its context). Mode 600: an MCP server's settings may
//! hold a token.

use serde_json::{json, Value};
use std::path::{Path, PathBuf};

/// The answer lengths, context sizes and memories offered (the file may say only these).
pub const ANSWER_LENGTHS: [u32; 4] = [256, 512, 1024, 2048];
/// 8192 is what a7a-llm served before the setting (its memory is reserved at start: about 1.2 GB per 8192 for the 4B
/// model); 16384 needs 1.2 GB more, which a7a-llm's memory check counts.
pub const CONTEXTS: [u32; 3] = [4096, 8192, 16384];
pub const MEMORIES: [u32; 6] = [1, 2, 3, 4, 6, 8];

/// What an MCP server reaches, which decides how a chat's one-source rule treats it (agent.rs): one that reads this
/// board counts as the board's tools, one that reaches the internet as the web's, and one that does both (a browser
/// that opens file:// pages, a files-and-fetch server) gets a chat to itself, nothing else in it before or after: it
/// can still carry out what it reads through its own tools, which no rule here can see (review 2026-11-19). "Both"
/// is the default: the owner says when a server is less.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Reach {
    Board,
    Web,
    #[default]
    Both,
}

impl Reach {
    pub const ALL: [Reach; 3] = [Reach::Board, Reach::Web, Reach::Both];
    fn key(self) -> &'static str {
        match self {
            Reach::Board => "board",
            Reach::Web => "web",
            Reach::Both => "both",
        }
    }
}

impl std::fmt::Display for Reach {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Reach::Board => "reads this board",
            Reach::Web => "reaches the internet",
            Reach::Both => "does both",
        })
    }
}

/// An MCP server: a program speaking the Model Context Protocol on its stdin and stdout, started as the owner.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Server {
    /// short, unique: it begins the names of its tools as the model sees them
    pub name: String,
    /// the command line, split as a shell would (quotes; no pipes, no variables)
    pub command: String,
    /// KEY=value words added to its environment (a token, say)
    pub env: String,
    pub reach: Reach,
    pub on: bool,
    /// its tools the owner switched off
    pub off_tools: Vec<String>,
}

/// Where an online assistant is: a provider, with what its API needs (prefs::Online).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Provider {
    #[default]
    Google,
    Brave,
    Mistral,
    Perplexity,
    OpenRouter,
    Groq,
    OpenAI,
    /// Cloudflare's Workers AI: OpenAI-style, at an address made of the owner's Account ID (a free plan)
    Cloudflare,
    /// any API that speaks OpenAI's chat completions, at an address the owner gives
    Other,
}

impl Provider {
    pub const ALL: [Provider; 9] = [Provider::Google, Provider::Cloudflare, Provider::Brave, Provider::Mistral,
                                   Provider::Perplexity, Provider::OpenRouter, Provider::Groq, Provider::OpenAI, Provider::Other];

    pub fn key(self) -> &'static str {
        match self {
            Provider::Google => "google",
            Provider::Brave => "brave",
            Provider::Mistral => "mistral",
            Provider::Perplexity => "perplexity",
            Provider::OpenRouter => "openrouter",
            Provider::Groq => "groq",
            Provider::OpenAI => "openai",
            Provider::Cloudflare => "cloudflare",
            Provider::Other => "other",
        }
    }

    /// A provider as the file names it (its key, any case). One it does not know is Other with no address, which
    /// asks nobody: taken for Google, a hand-edited "OpenAI" sent its key and the question there (review 2026-11-19).
    fn from_key(k: &str) -> Provider {
        let k = k.trim().to_lowercase();
        Provider::ALL.into_iter().find(|p| p.key() == k).unwrap_or(Provider::Other)
    }

    /// Its model when the owner names none: an alias that follows the provider's newest, where it has one (Google's
    /// model names change every few months); empty where the owner must choose.
    pub fn model(self) -> &'static str {
        match self {
            Provider::Google => "gemini-flash-latest",
            Provider::Brave => "brave",
            Provider::Mistral => "mistral-small-latest",
            Provider::Perplexity => "sonar",
            Provider::OpenRouter => "openrouter/auto",
            // the owner's best on the board (2026-11-22): fast, about 350 answers a day on the free plan
            Provider::Cloudflare => "@cf/openai/gpt-oss-120b",
            Provider::Groq | Provider::OpenAI | Provider::Other => "",
        }
    }

    /// Its OpenAI-style API (chat completions under it); Google's own API is not one, Other's is the owner's.
    pub fn url(self) -> &'static str {
        match self {
            Provider::Google | Provider::Cloudflare | Provider::Other => "",
            Provider::Brave => "https://api.search.brave.com/res/v1",
            Provider::Mistral => "https://api.mistral.ai/v1",
            Provider::Perplexity => "https://api.perplexity.ai",
            Provider::OpenRouter => "https://openrouter.ai/api/v1",
            Provider::Groq => "https://api.groq.com/openai/v1",
            Provider::OpenAI => "https://api.openai.com/v1",
        }
    }

    /// Where its key comes from, for the page.
    pub fn keys_at(self) -> &'static str {
        match self {
            Provider::Google => "aistudio.google.com (free, with limits)",
            Provider::Brave => "api-dashboard.search.brave.com (the Answers plan: paid)",
            Provider::Mistral => "console.mistral.ai",
            Provider::Perplexity => "perplexity.ai (paid)",
            Provider::OpenRouter => "openrouter.ai (some models free)",
            Provider::Groq => "console.groq.com",
            Provider::OpenAI => "platform.openai.com (paid)",
            Provider::Cloudflare => "dash.cloudflare.com (free: an API token from the \u{201c}Workers AI\u{201d} template)",
            Provider::Other => "the service's own site",
        }
    }
}

impl std::fmt::Display for Provider {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Provider::Google => "Google (Gemini, searches Google)",
            Provider::Brave => "Brave (Answers, searches the web)",
            Provider::Mistral => "Mistral",
            Provider::Perplexity => "Perplexity (searches the web)",
            Provider::OpenRouter => "OpenRouter",
            Provider::Groq => "Groq",
            Provider::OpenAI => "OpenAI",
            Provider::Cloudflare => "Cloudflare Workers AI (free)",
            Provider::Other => "Other (OpenAI-style API)",
        })
    }
}

/// An assistant on the internet: a provider's model, reached with the owner's key. Picked in the Assistant tab, or
/// asked by voice with "hey <name>". It has none of this board's tools, and what it is asked goes to its provider.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Online {
    /// a word or two of letters: the tab's list shows it, and "hey <name>" asks it by voice
    pub name: String,
    pub provider: Provider,
    /// empty: the provider's usual one (Provider::model)
    pub model: String,
    /// Other's API address (its chat completions under it); Cloudflare's Account ID (or a whole https:// address,
    /// an AI Gateway's); the others have their own
    pub url: String,
    pub key: String,
    pub on: bool,
}

impl Online {
    pub fn model(&self) -> &str {
        if self.model.trim().is_empty() { self.provider.model() } else { self.model.trim() }
    }

    pub fn url(&self) -> std::borrow::Cow<'_, str> {
        let own = self.url.trim().trim_end_matches('/');
        match self.provider {
            Provider::Other => own.into(),
            Provider::Cloudflare if own.is_empty() || own.contains("://") => own.into(),
            Provider::Cloudflare => format!("{CLOUDFLARE}{own}/ai/v1").into(),
            p => p.url().into(),
        }
    }
}

/// Workers AI's OpenAI-style API is this, the Account ID and /ai/v1.
pub const CLOUDFLARE: &str = "https://api.cloudflare.com/client/v4/accounts/";

/// What is wrong with a Cloudflare row's Account ID: 32 hexadecimal digits, from the dashboard's Account details
/// (an API token pasted there by mistake is the likely slip).
pub fn account_problem(o: &Online) -> Option<&'static str> {
    let id = o.url.trim().trim_end_matches('/');
    if o.provider != Provider::Cloudflare || id.contains("://") {
        return None;
    }
    if id.is_empty() {
        return Some("its Account ID is missing (dash.cloudflare.com, Account details)");
    }
    (!(id.len() == 32 && id.bytes().all(|b| b.is_ascii_hexdigit())))
        .then_some("its Account ID is 32 letters a-f and digits (dash.cloudflare.com, Account details): the token goes in the key field")
}

/// An address its key may go to: https, or plain http to this machine only (the key would cross the network
/// readable: review 2026-11-19). Empty: none yet (said when it is asked).
pub fn address_problem(url: &str) -> Option<&'static str> {
    let rest = url.strip_prefix("http://")?;
    let host = rest.split(['/', '?', '#']).next().unwrap_or("");
    let host = host.rsplit_once('@').map_or(host, |(_, h)| h);
    let host = if host.starts_with('[') { host.split(']').next().map(|h| &h[1..]).unwrap_or("") } else { host.split(':').next().unwrap_or("") };
    (!matches!(host, "localhost" | "127.0.0.1" | "::1"))
        .then_some("its address must begin with https:// (http:// only to this board itself): the key would cross the network unencrypted")
}

/// A key fit to send: plain printable characters only. One with an invisible character pasted with it (a soft
/// hyphen, a zero-width space) could not go in a header, and ended up shown whole in the error (review 2026-11-19).
pub fn key_problem(key: &str) -> Option<&'static str> {
    if key.is_empty() {
        return None;
    }
    (!key.bytes().all(|b| (0x21..=0x7e).contains(&b))).then_some("its key has a character that is not plain text (pasted with it?): paste it again")
}

/// An online assistant's name as it is said: lower-case words of letters, single spaces, at most three words and
/// 24 letters ("Google!" is "google", "My  Brave" is "my brave"); empty when nothing of it is left.
pub fn online_name(name: &str) -> String {
    let cleaned: String = name.to_lowercase().chars().map(|c| if c.is_ascii_lowercase() { c } else { ' ' }).collect();
    cleaned.split_whitespace().take(3).collect::<Vec<_>>().join(" ").chars().take(24).collect::<String>().trim().to_string()
}

#[derive(Debug, Clone, PartialEq)]
pub struct Prefs {
    pub instructions: String,
    /// the folders the file tools may read; empty: everywhere but secrets (agent::secret)
    pub folders: Vec<String>,
    /// why the folders setting cannot be used (in the file, but none of it reads; or the file does not read): the
    /// file tools then read nothing, rather than everywhere (review 2026-11-19). Not written to the file.
    pub folders_error: String,
    /// the tab's "web tools" switch
    pub web: bool,
    pub web_search: bool,
    pub fetch_url: bool,
    pub weather: bool,
    /// exchanges remembered (a question and its answer each)
    pub memory: u32,
    /// the most tokens an answer may have
    pub answer_length: u32,
    /// the model server's context, in tokens (from its next start)
    pub context: u32,
    pub mcp: Vec<Server>,
    pub online: Vec<Online>,
    /// who answers in the tab: "" this board's model, else an online assistant's name
    pub ask_with: String,
}

impl Default for Prefs {
    fn default() -> Self {
        Prefs { instructions: String::new(), folders: Vec::new(), folders_error: String::new(), web: true, web_search: true,
                fetch_url: true, weather: true, memory: 3, answer_length: 512, context: 8192, mcp: Vec::new(),
                online: Vec::new(), ask_with: String::new() }
    }
}

/// `name` as a tool-name prefix: lower case letters, digits, '_' and '-', at most 20 (an OpenAI function name may
/// have 64 characters, and the tool's own name follows it).
pub fn server_name(name: &str) -> String {
    name.trim().to_lowercase().chars().map(|c| if c.is_ascii_alphanumeric() || c == '-' { c } else { '_' }).take(20).collect()
}

impl Prefs {
    /// ~/.config/a7a-panel/assistant.json (XDG_CONFIG_HOME when set).
    pub fn path() -> PathBuf {
        let base = std::env::var("XDG_CONFIG_HOME").ok().filter(|v| !v.is_empty()).map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".config"));
        base.join("a7a-panel").join("assistant.json")
    }

    /// Tolerant: what is missing or of another type takes its default, a number not offered the nearest one offered.
    pub fn from_json(v: &Value) -> Prefs {
        let d = Prefs::default();
        let flag = |k: &str, def: bool| v[k].as_bool().unwrap_or(def);
        let nearest = |k: &str, choices: &[u32], def: u32| {
            v[k].as_u64().map(|n| *choices.iter().min_by_key(|c| (**c as i64 - n as i64).abs()).unwrap()).unwrap_or(def)
        };
        let strings = |x: &Value| x.as_array().map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect::<Vec<_>>()).unwrap_or_default();
        let mcp = v["mcp"].as_array().map(|a| a.iter().filter(|s| s.is_object()).map(|s| Server {
            name: s["name"].as_str().unwrap_or_default().to_string(),
            command: s["command"].as_str().unwrap_or_default().to_string(),
            env: s["env"].as_str().unwrap_or_default().to_string(),
            reach: match s["reach"].as_str() {
                Some("board") => Reach::Board,
                Some("web") => Reach::Web,
                _ => Reach::Both,
            },
            on: s["on"].as_bool().unwrap_or(true),
            off_tools: strings(&s["off_tools"]),
        }).collect()).unwrap_or_default();
        let online = v["online"].as_array().map(|a| a.iter().filter(|o| o.is_object()).map(|o| Online {
            name: o["name"].as_str().unwrap_or_default().to_string(),
            provider: Provider::from_key(o["provider"].as_str().unwrap_or_default()),
            model: o["model"].as_str().unwrap_or_default().to_string(),
            url: o["url"].as_str().unwrap_or_default().to_string(),
            key: o["key"].as_str().unwrap_or_default().to_string(),
            on: o["on"].as_bool().unwrap_or(true),
        }).collect()).unwrap_or_default();
        // folders named, but none of them usable (not a list, relative paths, "~user"): closed, not everywhere
        let named = &v["folders"];
        let folders = clean_folders(&strings(named));
        let folders_error = if !named.is_null() && !named.as_array().is_some_and(|a| a.is_empty()) && folders.is_empty() {
            format!("none of the folders reads ({named}): a folder is a full path or one under ~")
        } else {
            String::new()
        };
        Prefs {
            instructions: v["instructions"].as_str().unwrap_or_default().to_string(),
            folders,
            folders_error,
            web: flag("web", d.web),
            web_search: flag("web_search", d.web_search),
            fetch_url: flag("fetch_url", d.fetch_url),
            weather: flag("weather", d.weather),
            memory: nearest("memory", &MEMORIES, d.memory),
            answer_length: nearest("answer_length", &ANSWER_LENGTHS, d.answer_length),
            context: nearest("context", &CONTEXTS, d.context),
            mcp,
            online,
            ask_with: v["ask_with"].as_str().unwrap_or_default().to_string(),
        }
        .cleaned()
    }

    pub fn to_json(&self) -> Value {
        json!({
            "instructions": self.instructions, "folders": self.folders, "web": self.web, "web_search": self.web_search,
            "fetch_url": self.fetch_url, "weather": self.weather, "memory": self.memory,
            "answer_length": self.answer_length, "context": self.context,
            "mcp": self.mcp.iter().map(|s| json!({"name": s.name, "command": s.command, "env": s.env, "reach": s.reach.key(),
                                                   "on": s.on, "off_tools": s.off_tools})).collect::<Vec<_>>(),
            "online": self.online.iter().map(|o| json!({"name": o.name, "provider": o.provider.key(), "model": o.model,
                                                         "url": o.url, "key": o.key, "on": o.on})).collect::<Vec<_>>(),
            "ask_with": self.ask_with,
        })
    }

    /// As the tools use them: folders trimmed, without a trailing '/', each once, only absolute ones or ones under
    /// ~; servers without a command dropped, their names made tool-name prefixes and unique ("files", "files-2").
    pub fn cleaned(mut self) -> Prefs {
        self.folders = clean_folders(&self.folders);
        let mut seen: Vec<String> = Vec::new();
        self.mcp = self.mcp.iter().filter_map(as_saved).collect();
        for s in &mut self.mcp {
            let base = s.name.clone();
            let mut k = 2;
            while seen.contains(&s.name) {
                s.name = format!("{base}-{k}");
                k += 1;
            }
            seen.push(s.name.clone());
        }
        // online assistants: a name that can be said (the provider's when none is left), each once ("second google"),
        // the key and the rest trimmed; the tab's choice one of those switched on, else this board's model
        let mut names: Vec<String> = Vec::new();
        for o in &mut self.online {
            let base = match online_name(&o.name) {
                n if n.is_empty() => o.provider.key().to_string(),
                n => n,
            };
            o.name = base.clone();
            // numbered at the front: "brave two" begins with "brave", and the spotter names "hey brave" as soon as it
            // is said, so "two" went to the first brave (review 2026-11-19)
            for k in ["second", "third", "fourth", "fifth", "sixth"] {
                if !names.contains(&o.name) {
                    break;
                }
                o.name = format!("{k} {base}");
            }
            names.push(o.name.clone());
            o.key = o.key.trim().to_string();
            o.model = o.model.trim().to_string();
            o.url = o.url.trim().to_string();
        }
        let mut seen_names = std::collections::HashSet::new();
        self.online.retain(|o| seen_names.insert(o.name.clone())); // (a seventh "google": dropped)
        if !self.online.iter().any(|o| o.on && o.name == self.ask_with) {
            self.ask_with.clear();
        }
        self
    }

    /// The online assistant called `name` (as the tab and voice name it), switched on.
    pub fn online_named(&self, name: &str) -> Option<&Online> {
        self.online.iter().find(|o| o.on && o.name == name)
    }

    /// The owner's settings, or the defaults when there are none; when they do not read, the defaults with the file
    /// tools closed (their folders may have been the point of them) and the reason, for the tab.
    pub fn load(path: &Path) -> (Prefs, String) {
        let why = match std::fs::read_to_string(path) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return (Prefs::default(), String::new()),
            Err(e) => format!("{}: {e}", path.display()),
            Ok(s) => match serde_json::from_str::<Value>(&s) {
                Ok(v) if v.is_object() => return (Prefs::from_json(&v), String::new()),
                Ok(_) => format!("{}: not a JSON object", path.display()),
                Err(e) => format!("{}: {e}", path.display()),
            },
        };
        (Prefs { folders_error: format!("the settings do not read ({why})"), web: false, ..Prefs::default() }, why)
    }

    /// Whether these settings may be written back as they are (the tab's web switch does so): not while the folders
    /// could not be used, which written back would read as "none", that is everywhere (review 2026-11-19); the
    /// Settings page sets them anew instead.
    pub fn savable(&self) -> Result<(), String> {
        if self.folders_error.is_empty() {
            Ok(())
        } else {
            Err(self.folders_error.clone())
        }
    }

    /// The page's settings as they are saved: cleaned, the folders as the page lists them (so whatever the file had
    /// that could not be used is left behind), each server's command and environment readable.
    pub fn for_save(self, instructions: String, web: bool) -> Result<Prefs, String> {
        let p = Prefs { instructions, web, folders_error: String::new(), ..self }.cleaned();
        for sv in &p.mcp {
            split_command(&sv.command).and_then(|_| env_pairs(&sv.env)).map_err(|e| format!("the MCP server {}: {e}", sv.name))?;
        }
        for o in &p.online {
            if let Some(why) = key_problem(&o.key) {
                return Err(format!("{}: {why}", o.name));
            }
            if let Some(why) = address_problem(&o.url()).or_else(|| account_problem(o)) {
                return Err(format!("{}: {why}", o.name));
            }
        }
        Ok(p)
    }

    /// Whether who answers in the tab is another than before (gone, renamed, another provider, model, address or key:
    /// another key may be another account, and a row removed can hand its name to the next one): the chat then starts
    /// anew, as when the picker changes it (reviews 2026-11-19).
    pub fn answerer_changed(before: Option<&Online>, after: Option<&Online>) -> bool {
        match (before, after) {
            (None, None) => false,
            (Some(a), Some(b)) => (a.name.as_str(), a.provider, a.url(), a.model(), a.key.as_str())
                != (b.name.as_str(), b.provider, b.url(), b.model(), b.key.as_str()),
            _ => true,
        }
    }

    /// Whole or not at all (a temporary file moved into place), readable by the owner only.
    pub fn save(&self, path: &Path) -> Result<(), String> {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let dir = path.parent().ok_or("no folder for the settings")?;
        std::fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        let tmp = path.with_extension("json.tmp");
        let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&tmp)
            .map_err(|e| format!("{}: {e}", tmp.display()))?;
        f.write_all(format!("{:#}\n", self.to_json()).as_bytes()).map_err(|e| format!("{}: {e}", tmp.display()))?;
        f.sync_all().ok();
        std::fs::rename(&tmp, path).map_err(|e| format!("{}: {e}", path.display()))
    }
}

/// The folders as the tools use them: trimmed, without a trailing '/', each once, only absolute ones or ones under ~.
pub fn clean_folders(folders: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for f in folders {
        let f = f.trim();
        let f = if f.len() > 1 { f.trim_end_matches('/') } else { f };
        if (f.starts_with('/') || f == "~" || f.starts_with("~/")) && !out.iter().any(|g| g == f) {
            out.push(f.to_string());
        }
    }
    out
}

/// A server as it is saved, on its own: its name a tool-name prefix ("mcp" when nothing of it is left), its command
/// and environment trimmed; None without a command. (cleaned() makes the names unique on top.)
pub fn as_saved(s: &Server) -> Option<Server> {
    if s.command.trim().is_empty() {
        return None;
    }
    let name = match server_name(&s.name) {
        n if n.trim_matches('_').is_empty() => "mcp".to_string(),
        n => n,
    };
    Some(Server { name, command: s.command.trim().to_string(), env: s.env.trim().to_string(), ..s.clone() })
}

/// A command line in words, as a shell splits it: blanks separate them, '…' keeps everything, "…" keeps all but \"
/// and \\, a backslash outside quotes keeps the next character. No variables, globs or pipes: the program is run
/// directly.
pub fn split_command(line: &str) -> Result<Vec<String>, String> {
    let mut words = Vec::new();
    let mut cur = String::new();
    let mut started = false;
    let mut chars = line.chars();
    while let Some(c) = chars.next() {
        match c {
            ' ' | '\t' | '\n' => {
                if started {
                    words.push(std::mem::take(&mut cur));
                    started = false;
                }
            }
            '\'' => {
                started = true;
                loop {
                    match chars.next() {
                        Some('\'') => break,
                        Some(x) => cur.push(x),
                        None => return Err("a ' is not closed".into()),
                    }
                }
            }
            '"' => {
                started = true;
                loop {
                    match chars.next() {
                        Some('"') => break,
                        Some('\\') => match chars.next() {
                            Some(x @ ('"' | '\\')) => cur.push(x),
                            Some(x) => {
                                cur.push('\\');
                                cur.push(x);
                            }
                            None => return Err("a \" is not closed".into()),
                        },
                        Some(x) => cur.push(x),
                        None => return Err("a \" is not closed".into()),
                    }
                }
            }
            '\\' => {
                started = true;
                match chars.next() {
                    Some(x) => cur.push(x),
                    None => return Err("a \\ at the end".into()),
                }
            }
            x => {
                started = true;
                cur.push(x);
            }
        }
    }
    if started {
        words.push(cur);
    }
    Ok(words)
}

/// A server's environment words as (key, value) pairs; a word without '=' or with a key that is not a name is an
/// error.
pub fn env_pairs(env: &str) -> Result<Vec<(String, String)>, String> {
    split_command(env)?
        .into_iter()
        .map(|w| match w.split_once('=') {
            Some((k, v)) if !k.is_empty() && k.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') && !k.starts_with(|c: char| c.is_ascii_digit()) => {
                Ok((k.to_string(), v.to_string()))
            }
            _ => Err(format!("{w:?} is not KEY=value")),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_defaults_are_what_the_tab_did_before() {
        let d = Prefs::default();
        assert_eq!((d.web, d.web_search, d.fetch_url, d.weather), (true, true, true, true));
        assert_eq!((d.memory, d.answer_length, d.context), (3, 512, 8192)); // CHAT_MEMORY 6 messages, max_tokens 512, a7a-llm's 8192
        assert!(d.instructions.is_empty() && d.folders.is_empty() && d.mcp.is_empty());
        assert_eq!(Prefs::from_json(&json!({})), d);
    }

    #[test]
    fn odd_values_are_tamed() {
        let p = Prefs::from_json(&json!({"memory": 100, "answer_length": 700, "context": 1, "web": "yes",
            "folders": ["/home/radxa/Documents/", "relative/path", " ~/Music ", "/home/radxa/Documents", 5, "/"],
            "mcp": [{"name": "My Files!", "command": " npx -y server "}, {"name": "my files", "command": "x"},
                    {"name": "", "command": "y", "reach": "board", "on": false, "off_tools": ["a", 1]}, {"command": ""}, 3]}));
        assert_eq!((p.memory, p.answer_length, p.context, p.web), (8, 512, 4096, true));
        assert_eq!(p.folders, ["/home/radxa/Documents", "~/Music", "/"]);
        let names: Vec<&str> = p.mcp.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, ["my_files_", "my_files", "mcp"]);
        assert_eq!(p.mcp[0].command, "npx -y server");
        assert_eq!((p.mcp[2].reach, p.mcp[2].on, p.mcp[2].off_tools.clone()), (Reach::Board, false, vec!["a".to_string()]));
        assert_eq!(p.mcp[0].reach, Reach::Both); // the default: a chat to itself, until the owner says it is less
        assert_eq!(p.folders_error, ""); // some of the folders read
    }

    #[test]
    fn folders_named_but_none_usable_close_the_file_tools() {
        for bad in [json!(["Documents"]), json!(["~radxa/Documents", "  "]), json!("~/Documents"), json!({"a": 1}), json!([5])] {
            let p = Prefs::from_json(&json!({"folders": bad}));
            assert!(p.folders.is_empty() && p.folders_error.contains("none of the folders reads"), "{bad}: {}", p.folders_error);
        }
        // an empty list, or none at all: everywhere but the secrets, as the page says
        assert_eq!(Prefs::from_json(&json!({"folders": []})).folders_error, "");
        assert_eq!(Prefs::from_json(&json!({})).folders_error, "");
        // some usable: those, and no error (the others are dropped: fewer folders, not more)
        let p = Prefs::from_json(&json!({"folders": ["Documents", "/srv/ok"]}));
        assert_eq!((p.folders, p.folders_error), (vec!["/srv/ok".to_string()], String::new()));
        // what the file says is never "folders_error" itself: it is not written
        assert!(Prefs::default().to_json().get("folders_error").is_none());
    }

    #[test]
    fn unusable_folders_are_not_written_back_as_none() {
        let p = Prefs::from_json(&json!({"folders": ["Documents"], "web": true}));
        assert!(p.savable().is_err()); // the web switch must not save them (they would read as "everywhere")
        // the page saves them anew: its list (here none, chosen there), and the error gone
        let saved = p.clone().for_save("be brief".into(), false).unwrap();
        assert_eq!((saved.folders_error.as_str(), saved.folders.len(), saved.web, saved.instructions.as_str()), ("", 0, false, "be brief"));
        assert!(saved.savable().is_ok());
        let mut with_folder = p;
        with_folder.folders = vec!["/home/radxa/Documents".into()];
        assert_eq!(with_folder.for_save(String::new(), true).unwrap().folders, ["/home/radxa/Documents"]);
        // a server whose command does not read is said, and nothing saved
        let bad = Prefs { mcp: vec![Server { name: "x".into(), command: "a 'b".into(), ..Default::default() }], ..Prefs::default() };
        assert_eq!(bad.for_save(String::new(), true).unwrap_err(), "the MCP server x: a ' is not closed");
    }

    #[test]
    fn online_assistants_are_named_as_said_and_kept_apart() {
        let p = Prefs::from_json(&json!({"online": [
            {"name": "Google!", "provider": "google", "key": " k1 "},
            {"name": "google", "provider": "brave", "key": "k2"},
            {"name": "", "provider": "brave"},
            {"name": "my   Big Assistant here", "provider": "nonsense", "on": false},
            5], "ask_with": "second google"}));
        let names: Vec<&str> = p.online.iter().map(|o| o.name.as_str()).collect();
        assert_eq!(names, ["google", "second google", "brave", "my big assistant"]);
        let providers: Vec<Provider> = p.online.iter().map(|o| o.provider).collect();
        // one it does not know is Other, with no address: it asks nobody (not Google, with another's key)
        assert_eq!(providers, [Provider::Google, Provider::Brave, Provider::Brave, Provider::Other]);
        assert_eq!(p.online[3].url(), "");
        let loose = Prefs::from_json(&json!({"online": [{"name": "o", "provider": " OpenAI "}]}));
        assert_eq!(loose.online[0].provider, Provider::OpenAI);
        assert_eq!((p.online[0].key.as_str(), p.ask_with.as_str()), ("k1", "second google"));
        assert_eq!(p.online_named("my big assistant"), None); // switched off
        // the tab's choice of one switched off, or of none there, is this board's model
        assert_eq!(Prefs::from_json(&json!({"online": [{"name": "x", "on": false}], "ask_with": "x"})).ask_with, "");
        assert_eq!(Prefs::from_json(&json!({"ask_with": "nobody"})).ask_with, "");
        // read back as written, keys and all (the file is the owner's alone: mode 600)
        assert_eq!(Prefs::from_json(&p.to_json()), p);
        // a model and an address when none are given
        let g = Online { provider: Provider::Google, ..Default::default() };
        assert_eq!((g.model(), g.url().as_ref()), ("gemini-flash-latest", ""));
        let o = Online { provider: Provider::Other, url: " https://example.org/v1/ ".into(), model: " m ".into(), ..Default::default() };
        assert_eq!((o.model(), o.url().as_ref()), ("m", "https://example.org/v1"));
        assert_eq!(Online { provider: Provider::Brave, ..Default::default() }.url(), "https://api.search.brave.com/res/v1");
        assert_eq!(online_name(" Hey, 2 BRAVE!! "), "hey brave");
    }

    #[test]
    fn cloudflare_from_its_account_id() {
        let id = "0123456789abcdef0123456789ABCDEF";
        let o = Online { name: "cloud".into(), provider: Provider::Cloudflare, url: format!(" {id} "), key: "t".into(), on: true, ..Default::default() };
        assert_eq!(o.url(), format!("https://api.cloudflare.com/client/v4/accounts/{id}/ai/v1"));
        assert_eq!(o.model(), "@cf/openai/gpt-oss-120b");
        assert_eq!(account_problem(&o), None);
        // an AI Gateway's whole address is taken as it is
        let gw = Online { url: "https://gateway.ai.cloudflare.com/v1/acc/gw/workers-ai/v1/".into(), ..o.clone() };
        assert_eq!((gw.url().as_ref(), account_problem(&gw)), ("https://gateway.ai.cloudflare.com/v1/acc/gw/workers-ai/v1", None));
        // nothing, or the token pasted in the wrong field, is said before Save
        for bad in ["", "not-an-id", "0123456789abcdef0123456789abcde", "0123456789abcdef0123456789abcdefg"] {
            let b = Online { url: bad.into(), ..o.clone() };
            assert!(account_problem(&b).is_some(), "{bad:?}");
            assert!(Prefs { online: vec![b], ..Default::default() }.for_save(String::new(), true).is_err(), "{bad:?}");
        }
        assert!(Prefs { online: vec![o.clone()], ..Default::default() }.for_save(String::new(), true).is_ok());
        // read back as written (trimmed, as every field is), under its own key
        let p = Prefs { online: vec![Online { url: id.into(), ..o }], ..Default::default() };
        assert_eq!(p.to_json()["online"][0]["provider"], "cloudflare");
        assert_eq!(Prefs::from_json(&p.to_json()), p);
    }

    #[test]
    fn a_key_goes_unencrypted_only_to_this_board() {
        for ok in ["https://example.org/v1", "http://127.0.0.1:8080/v1", "http://localhost/v1", "http://[::1]:8080/v1", ""] {
            assert_eq!(address_problem(ok), None, "{ok}");
        }
        for bad in ["http://example.org/v1", "http://192.168.1.10:8080/v1", "http://127.0.0.1.evil.org/v1", "http://localhost@evil.org/v1"] {
            assert!(address_problem(bad).is_some(), "{bad}");
        }
    }

    #[test]
    fn online_names_are_the_voices_too() {
        // the entries of test_a7a_voice.py's test_their_names_from_the_panels_settings: a7a-voice names them as this
        // does, or "hey second google" would ask for a name the panel does not have
        let p = Prefs::from_json(&json!({"online": [
            {"name": "google", "key": "k1", "on": true}, {"name": "brave", "key": "k2"},
            {"name": "mistral", "key": "", "on": true}, {"name": "groq", "key": "k3", "on": false},
            {"name": "Google", "key": "k4"}, {"name": "", "key": "k5"}, 5, {"key": "k6"},
            {"name": "", "provider": "Brave", "key": "k7"}, {"name": "GPT-4o Mini Turbo Max", "key": "k8", "on": "yes"},
            {"name": "a very long name for an assistant", "key": " k9 "},
            {"name": "supercalifragilistic expialidocious", "key": "k10"}, {"name": "x", "key": 5},
            {"name": "", "provider": "Cloudflare", "key": "k11"}]}));
        let said: Vec<&str> = p.online.iter().filter(|o| o.on && !o.key.is_empty()).map(|o| o.name.as_str()).collect();
        assert_eq!(said, ["google", "brave", "second google", "other", "second other", "second brave", "gpt o mini", "a very long",
                          "supercalifragilistic exp", "cloudflare"]);
        let seven = Prefs::from_json(&json!({"online": vec![json!({"name": "google", "key": "k"}); 7]}));
        let names: Vec<&str> = seven.online.iter().map(|o| o.name.as_str()).collect();
        assert_eq!(names, ["google", "second google", "third google", "fourth google", "fifth google", "sixth google"]);
    }

    #[test]
    fn keys_are_plain_text_and_a_new_answerer_is_seen() {
        assert_eq!(key_problem("AIza-Sy_09azAZ"), None);
        assert_eq!(key_problem(""), None);
        for bad in ["sk-proj-SECRET\u{ad}KEY", "a\u{200b}b", "with space", "tab\tkey", "é"] {
            assert!(key_problem(bad).is_some(), "{bad:?}");
        }
        let p = Prefs { online: vec![Online { name: "g".into(), key: "sk-\u{ad}x".into(), on: true, ..Default::default() }], ..Prefs::default() };
        let e = p.for_save(String::new(), true).unwrap_err();
        assert!(e.starts_with("g: its key has a character") && !e.contains("sk-"), "{e}"); // never the key itself
        let g = Online { name: "google".into(), provider: Provider::Google, key: "k1".into(), on: true, ..Default::default() };
        let rekeyed = Online { key: "k2".into(), ..g.clone() };
        let renamed = Online { name: "gemini".into(), ..g.clone() };
        let other_model = Online { model: "gemini-3.6-flash".into(), ..g.clone() };
        assert!(Prefs::answerer_changed(Some(&g), Some(&rekeyed))); // a new key: maybe another account
        assert!(!Prefs::answerer_changed(Some(&g), Some(&g.clone())));
        assert!(Prefs::answerer_changed(Some(&g), Some(&renamed)) && Prefs::answerer_changed(Some(&g), Some(&other_model)));
        assert!(Prefs::answerer_changed(Some(&g), None) && Prefs::answerer_changed(None, Some(&g)));
        assert!(!Prefs::answerer_changed(None, None));
    }

    #[test]
    fn a_server_as_saved_on_its_own() {
        let s = Server { name: " My Files! ".into(), command: "  npx -y x  ".into(), env: " A=1 ".into(), ..Default::default() };
        let saved = as_saved(&s).unwrap();
        assert_eq!((saved.name.as_str(), saved.command.as_str(), saved.env.as_str()), ("my_files_", "npx -y x", "A=1"));
        assert_eq!(as_saved(&Server { name: "???".into(), command: "x".into(), ..Default::default() }).unwrap().name, "mcp");
        assert!(as_saved(&Server { name: "a".into(), command: "  ".into(), ..Default::default() }).is_none());
        // each reach read back as written
        for r in Reach::ALL {
            let p = Prefs::from_json(&Prefs { mcp: vec![Server { name: "a".into(), command: "x".into(), reach: r, ..Default::default() }], ..Prefs::default() }.to_json());
            assert_eq!(p.mcp[0].reach, r);
        }
    }

    #[test]
    fn a_name_used_twice_is_numbered() {
        let p = Prefs::from_json(&json!({"mcp": [{"name": "fetch", "command": "a"}, {"name": "Fetch", "command": "b"}, {"name": "fetch", "command": "c"}]}));
        let names: Vec<&str> = p.mcp.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, ["fetch", "fetch-2", "fetch-3"]);
    }

    #[test]
    fn saved_and_read_back_whole_and_private() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("a7a-prefs-{}", std::process::id()));
        let path = dir.join("a7a-panel").join("assistant.json");
        let mut p = Prefs { instructions: "Answer in two sentences.\nUse metric units.".into(), folders: vec!["~/Documents".into()],
                            web_search: false, memory: 4, context: 16384, ..Prefs::default() };
        p.mcp.push(Server { name: "github".into(), command: "npx -y @modelcontextprotocol/server-github".into(),
                            env: "GITHUB_TOKEN=abc".into(), reach: Reach::Web, on: true, off_tools: vec!["create_issue".into()] });
        p.save(&path).unwrap();
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        assert!(!path.with_extension("json.tmp").exists());
        assert_eq!(Prefs::load(&path), (p.clone(), String::new()));
        std::fs::write(&path, "{broken").unwrap();
        let (q, why) = Prefs::load(&path);
        assert!(why.contains("assistant.json"), "{why}");
        // the defaults, with the file tools closed: the folders may have been the point of the settings
        assert!(q.folders_error.contains("do not read"), "{}", q.folders_error);
        assert!(!q.web); // the web tools too: they may have been off on purpose
        assert_eq!(Prefs { folders_error: String::new(), web: true, ..q }, Prefs::default());
        assert_eq!(Prefs::load(&dir.join("none.json")), (Prefs::default(), String::new()));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn command_lines_split_as_a_shell_would() {
        assert_eq!(split_command("npx -y @mcp/server-filesystem /home/radxa").unwrap(), ["npx", "-y", "@mcp/server-filesystem", "/home/radxa"]);
        assert_eq!(split_command(r#"uvx  'a b' "c \"d\" \e" f\ g ''"#).unwrap(), ["uvx", "a b", "c \"d\" \\e", "f g", ""]);
        assert_eq!(split_command("  ").unwrap(), Vec::<String>::new());
        assert!(split_command("a 'b").is_err() && split_command("a \"b").is_err() && split_command("a\\").is_err());
        assert_eq!(env_pairs("GITHUB_TOKEN=abc NAME='x y' EMPTY=").unwrap(),
                   [("GITHUB_TOKEN".to_string(), "abc".to_string()), ("NAME".into(), "x y".into()), ("EMPTY".into(), String::new())]);
        assert!(env_pairs("novalue").is_err() && env_pairs("1X=a").is_err() && env_pairs("A-B=c").is_err());
        assert_eq!(env_pairs("").unwrap(), Vec::new());
    }
}
