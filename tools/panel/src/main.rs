//! a7a-panel: the clock, power and hardware panel of the Radxa Cubie A7A Fedora image.
//! Overview: every clock domain live, with a minute of history. Controls: every clock setting of a7a-clock (the old
//! Qt window a7a-clock-gui is gone). Hardware: the programs running, and which of them use the GPU, the display and
//! the NPU.
//! Assistant: a chat with the model that runs on this board (a7a-llm), which can look at the board through read-only
//! tools (agent.rs), with the owner's settings (prefs.rs: instructions, folders, web tools, MCP servers in mcp.rs,
//! memory and context). Voice: commands said to the board's microphone after a wake phrase, typed and switched on here
//! (a7a-voice, voice.rs).
//!   a7a-panel                 on the board
//!   a7a-panel --demo          generated values (also chosen by itself off the board)
//!   a7a-panel --screenshot F [--tab controls|hardware|assistant|settings|voice|guide] [--size WxH]
//!                             after three seconds, write the window as raw RGBA to F and its size to F.size, quit
//!                             (settings: the Assistant's settings page; an editor's text is missing from iced's
//!                             screenshots, though not from the window)
//!   a7a-panel --ask "Q"       the Assistant without a window: one question, the tools it used and the answer on
//!                             stdout (for a test over ssh)
//!   a7a-panel --answer "Q"    the same, the answer alone (the Assistant by voice: a7a-voice ask)
//!   ... --assistant NAME      with --ask or --answer: one of the online assistants answers (online.rs)

mod agent;
mod guide;
mod mcp;
mod online;
mod prefs;
mod sys;
mod ui;
mod voice;

use iced::widget::{button, canvas, column, container, markdown, pick_list, row, scrollable, space, text, text_editor, text_input, toggler, Column};
use iced::{Alignment, Color, Element, Fill, Length, Subscription, Task};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

const HISTORY: usize = 60;
const GPU_CLOCKS: [u32; 6] = [400, 600, 800, 1008, 1200, 1300];
/// The tab's choice of who answers, for this board's own model.
const BOARD_MODEL: &str = "this board's model";
const SUGGESTIONS: [&str; 3] = ["How hot is the board right now?", "What is using the most memory?", "Which kernel and Fedora release is this?"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Tab {
    Overview,
    Controls,
    Hardware,
    Assistant,
    Voice,
}

/// One entry of the Assistant's transcript.
enum Line {
    User(String),
    /// The answer as it streams in: markdown, parsed as it grows.
    Answer(markdown::Content, String),
    Tool { audit: String, observation: String },
    Note(String, Color),
}

#[derive(Debug, Clone)]
enum Message {
    Tick,
    Privileged(sys::Privileged),
    GpuStat(sys::GpuStat),
    Wifi(sys::Wifi),
    Processes(Vec<sys::Process>, sys::ProcTicks),
    Tab(Tab),
    Power(&'static str),
    Run(Vec<String>),
    Done(String),
    /// the 3.5 mm jack, as `a7a-clock codec state` says
    Jack(sys::Jack),
    /// the jack's button: `a7a-clock codec <codec>`, then the state asked again
    CodecSet(sys::Codec),
    CodecDone(String, sys::Jack),
    Governor(String),
    CapLittle(u32),
    CapBig(u32),
    ApplyCaps,
    GpuClock(u32),
    ApplyGpu,
    Shot(iced::window::Screenshot),
    // the Assistant
    Input(String),
    Ask(String),
    Send,
    StopAnswer,
    NewChat,
    Agent(u64, agent::Event),
    LlmHealth(&'static str),
    LlmStart,
    LlmStop,
    LlmDone(String),
    LlmGet,
    LlmGot(String),
    Model(String),
    Web(bool),
    Link(markdown::Uri),
    // answers read aloud (through a7a-voice)
    /// whether answers are read aloud, and the generation it was asked in (see Panel::speech_gen)
    Speech(u64, voice::Speech),
    Speak(bool),
    SpeakVoice(voice::Voice),
    SpeakDone(String),
    Hush,
    Hushed(String),
    /// one of the tab's answers has been read (or was stopped): `a7a-voice say`'s answer
    Spoken(String),
    /// the window's close button: an answer of the tab's being read stops first
    Close,
    Exit,
    // the Assistant's settings (prefs.rs)
    SettingsOpen,
    SettingsCancel,
    SettingsSave,
    Instructions(text_editor::Action),
    FolderInput(String),
    FolderAdd,
    FolderRemove(usize),
    WebSearch(bool),
    FetchUrl(bool),
    Weather(bool),
    Memory(u32),
    AnswerLength(u32),
    Context(u32),
    McpAdd,
    McpRemove(usize),
    McpName(usize, String),
    McpCommand(usize, String),
    McpEnv(usize, String),
    McpReach(usize, prefs::Reach),
    McpOn(usize, bool),
    McpLoad(usize),
    /// a server's tools, for its switches, by mcp_key
    McpLoaded(String, Result<Vec<mcp::Tool>, String>),
    McpTool(usize, String, bool),
    // online assistants (prefs::Online)
    /// who answers in the tab: "" this board's model, else an online assistant's name
    AskWith(String),
    OnlineAdd,
    OnlineRemove(usize),
    OnlineName(usize, String),
    OnlineProvider(usize, prefs::Provider),
    OnlineModel(usize, String),
    OnlineUrl(usize, String),
    OnlineKey(usize, String),
    OnlineOn(usize, bool),
    OnlineTest(usize),
    /// a test's answer: its row, and which test it was (Panel::online_testing)
    OnlineTested(usize, u64, Result<String, String>),
    // the Voice tab
    /// the listener's state, and the save generation it was asked in (see Panel::voice_gen)
    VoiceStatus(u64, voice::Status),
    VoiceWake(String),
    VoiceAssistant(String),
    VoiceSay(usize, String),
    VoiceRun(usize, String),
    VoiceAdd,
    VoiceRemove(usize),
    VoiceSave,
    VoiceSaved(String),
    VoiceTest(usize),
    /// a test's answer, after the save that went first (when there were changes)
    VoiceTested(Option<String>, String),
    /// the menu's applications and the common commands found again (saving what is typed first)
    VoiceScan,
    VoiceFoundSay(usize, String),
    VoiceFoundOn(usize, bool),
    VoiceFoundTest(usize),
    /// the found applications' commands shown or folded away (more than a hundred rows)
    VoiceShowApps(bool),
    VoiceOn(bool),
    VoiceSetup,
    VoiceDone(String),
    // Getting started (guide.rs)
    /// the guide opened or closed
    Guide(bool),
    /// whether it opens with the panel (its box: "Don't show this when the panel opens")
    GuideAtStart(bool),
    /// a step's button: that tab, the guide closed
    GuideGo(Tab),
    /// a step's button: the Assistant's settings page (the cloud assistants are on it)
    GuideSettings,
    /// a cloud assistant's Add: the settings page with a new row of that provider
    GuideAddOnline(prefs::Provider),
}

struct Panel {
    demo: bool,
    t: f32,
    image: sys::Image,
    snap: sys::Snapshot,
    privileged: sys::Privileged,
    prev_cpu: sys::CpuTimes,
    procs: Vec<sys::Process>,
    proc_ticks: sys::ProcTicks,
    proc_at: Instant,
    priv_pending: bool,
    gpu: sys::GpuStat,
    gpu_pending: bool,
    wifi: sys::Wifi,
    wifi_pending: bool,
    proc_pending: bool,
    ticks: u64,
    hist_big: VecDeque<f32>,
    hist_little: VecDeque<f32>,
    hist_temp: VecDeque<f32>,
    hist_mem: VecDeque<f32>,
    hist_gpu: VecDeque<f32>,
    tab: Tab,
    log: VecDeque<String>,
    busy: bool,
    cap_little: Option<u32>,
    cap_big: Option<u32>,
    gpu_clock: u32,
    screenshot: Option<String>,
    // the Assistant
    lines: Vec<Line>,
    history: Vec<Value>,
    input: String,
    /// A question typed while the server was down: sent once the model is loaded.
    waiting: Option<String>,
    answering: bool,
    asked_at: Instant,
    /// Each answer's events carry its number; a stopped answer's late events are dropped.
    run: u64,
    stop: Arc<AtomicBool>,
    /// What this chat has looked at, this board or the web: it may use one of the two (agent.rs). A new chat, a new one.
    taint: Arc<agent::Taint>,
    llm: &'static str,
    llm_pending: bool,
    llm_busy: bool,
    /// `a7a-llm get` running: how far it is (from the part file's size), updated every tick
    download: Option<f32>,
    models: Vec<String>,
    model: Option<String>,
    web: bool,
    rate: Option<(f32, u32)>,
    /// answers read aloud or not, the voice, something being read: `a7a-voice speak`, asked while the tab is open
    speech: voice::Speech,
    speech_pending: bool,
    /// speak on or off, or a new voice, at work (the first "on" downloads the voice): the switch shows what was
    /// asked for meanwhile
    speech_busy: bool,
    speech_want: Option<bool>,
    /// one more with every change of the switch or the voice: an answer asked for before it carries the settings
    /// from before it, and must not put them back (review 2026-11-19)
    speech_gen: u64,
    /// the tab's answers being read (their `a7a-voice say` running): one may still be ending as the next begins
    reading: usize,
    /// the owner's settings for the Assistant (prefs.rs), as saved; why they did not read, if they did not
    prefs: prefs::Prefs,
    prefs_error: String,
    /// the MCP servers the tab has started, kept between questions
    pool: Arc<mcp::Pool>,
    /// the settings page open in place of the chat, with what is being changed (saved by Save, dropped by Cancel)
    settings_open: bool,
    prefs_edit: prefs::Prefs,
    instructions: text_editor::Content,
    folder_input: String,
    /// servers' tools asked for on the page (Load its tools), by mcp_key
    mcp_found: HashMap<String, Result<Vec<mcp::Tool>, String>>,
    mcp_loading: HashSet<String>,
    /// online assistants' tests on the settings page, by their place in the list: a row's result goes when the row is
    /// changed, and an answer counts only for the test its row still waits for (by number, online_tests the last)
    online_tested: HashMap<usize, Result<String, String>>,
    online_testing: HashMap<usize, u64>,
    online_tests: u64,
    /// why Save did not save (shown beside it)
    settings_error: String,
    // the Voice tab
    voice: voice::Status,
    /// a status has come in since the panel started (before that the tab says it is asking)
    voice_known: bool,
    /// the editor holds settings read from a good file (kept while the file does not read)
    voice_known_config: bool,
    /// the settings as typed in the tab: the listener's own until something is changed, then these until Save
    voice_edit: voice::Config,
    voice_dirty: bool,
    voice_pending: bool,
    /// save, test, on, off or setup running
    voice_busy: bool,
    /// one more with every save: a status asked for before a save carries the settings from before it, and must not
    /// put them back into the editor when it comes in after the save
    voice_gen: u64,
    /// the found applications' commands are shown (folded away at first)
    voice_show_apps: bool,
    // Getting started (guide.rs)
    guide_open: bool,
    /// as saved in panel.json: the guide opens with the panel
    guide_at_start: bool,
    /// why the box's choice was not saved
    guide_error: String,
    /// the 3.5 mm jack's codec (Controls, and the guide when something is to be done): asked once at the start and
    /// after its button, not every tick (it reads this boot's kernel log)
    jack: sys::Jack,
    /// the switch's last words when the jack still wants it after its button (shown beside the button)
    jack_error: String,
}

/// Runs `f` on a thread of its own: a7a-clock can take minutes (the stress test), and the window must stay alive.
fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> impl std::future::Future<Output = T> {
    let (tx, rx) = iced::futures::channel::oneshot::channel();
    std::thread::spawn(move || {
        let _ = tx.send(f());
    });
    async move { rx.await.expect("the worker thread ended without an answer") }
}

fn push(h: &mut VecDeque<f32>, v: f32) {
    if h.len() == HISTORY {
        h.pop_front();
    }
    h.push_back(v);
}

impl Panel {
    fn new(demo: bool, screenshot: Option<String>, tab: Tab) -> (Self, Task<Message>) {
        let panel = Panel {
            demo,
            t: 0.0,
            image: if demo { sys::Image { variant: "oc (demo)".into(), kernel: "6.6.98-a7a.202609091931".into() } } else { sys::image() },
            snap: sys::Snapshot::default(),
            privileged: sys::Privileged::default(),
            prev_cpu: sys::cpu_times(),
            procs: Vec::new(),
            proc_ticks: Default::default(),
            proc_at: Instant::now(),
            priv_pending: false,
            gpu: sys::GpuStat::default(),
            gpu_pending: false,
            wifi: sys::Wifi::default(),
            wifi_pending: false,
            proc_pending: false,
            ticks: 0,
            hist_big: VecDeque::new(),
            hist_little: VecDeque::new(),
            hist_temp: VecDeque::new(),
            hist_mem: VecDeque::new(),
            hist_gpu: VecDeque::new(),
            tab,
            log: VecDeque::new(),
            busy: false,
            cap_little: None,
            cap_big: None,
            gpu_clock: 1008,
            screenshot,
            lines: Vec::new(),
            history: vec![json!({"role": "system", "content": agent::SYSTEM_PROMPT})],
            input: String::new(),
            waiting: None,
            answering: false,
            asked_at: Instant::now(),
            run: 0,
            stop: Arc::new(AtomicBool::new(false)),
            taint: Arc::new(agent::Taint::default()),
            llm: "unknown",
            llm_pending: false,
            llm_busy: false,
            download: None,
            models: agent::models(),
            model: None,
            web: true,
            rate: None,
            speech: if demo { voice::speech_demo() } else { voice::Speech::default() },
            speech_pending: false,
            speech_busy: false,
            speech_want: None,
            speech_gen: 0,
            reading: 0,
            prefs: prefs::Prefs::default(),
            prefs_error: String::new(),
            pool: Arc::new(mcp::Pool::default()),
            settings_open: false,
            prefs_edit: prefs::Prefs::default(),
            instructions: text_editor::Content::new(),
            folder_input: String::new(),
            mcp_found: HashMap::new(),
            mcp_loading: HashSet::new(),
            online_tested: HashMap::new(),
            online_testing: HashMap::new(),
            online_tests: 0,
            settings_error: String::new(),
            voice: if demo { voice::demo() } else { voice::Status::default() },
            voice_known: demo,
            voice_known_config: demo,
            voice_edit: if demo { voice::demo().config } else { voice::Config::default() },
            voice_dirty: false,
            voice_pending: false,
            voice_busy: false,
            voice_gen: 0,
            voice_show_apps: false,
            guide_open: false,
            guide_at_start: guide::at_start(&guide::path()),
            guide_error: String::new(),
            jack: if demo { sys::Jack::Ok(sys::Codec::Ac101) } else { sys::Jack::Unknown },
            jack_error: String::new(),
        };
        let mut panel = panel;
        panel.model = panel.models.iter().find(|m| m.starts_with("Qwen3-4B")).or(panel.models.first()).cloned();
        if !demo {
            (panel.prefs, panel.prefs_error) = prefs::Prefs::load(&prefs::Prefs::path());
            panel.web = panel.prefs.web;
            if !panel.prefs_error.is_empty() {
                panel.lines.push(Line::Note(format!("The Assistant's settings do not read ({}): the defaults are used, its file tools and web tools off, until Settings saves them again.", panel.prefs_error), ui::WARN));
            } else if !panel.prefs.folders_error.is_empty() {
                panel.lines.push(Line::Note(format!("The Assistant's file tools are off: {}. Set its folders in Settings.", panel.prefs.folders_error), ui::WARN));
            }
        }
        // the Voice tab's look in the demo: online assistants as on the owner's board (Cloudflare Workers AI)
        if demo && tab == Tab::Voice {
            panel.models = vec![agent::DEFAULT_MODEL.into()];
            let cf = "0123456789abcdef0123456789abcdef".to_string();     // an Account ID (made up)
            panel.prefs.online = vec![
                prefs::Online { name: "cloud".into(), provider: prefs::Provider::Cloudflare, model: String::new(), url: cf.clone(), key: "demo".into(), on: true },
                prefs::Online { name: "nova".into(), provider: prefs::Provider::Cloudflare, model: "@cf/qwen/qwen3.8-27b".into(), url: cf, key: "demo".into(), on: true },
            ];
        }
        // a look at the tab without a model: `--demo --tab assistant` opens on a made-up exchange
        if demo && tab == Tab::Assistant {
            panel.demo_chat();
        }
        // the guide opens with the panel until its box is ticked (never under --screenshot but for `--tab guide`)
        panel.guide_open = panel.guide_at_start && panel.screenshot.is_none();
        let task = if panel.screenshot.is_some() {
            Task::perform(async { std::thread::sleep(Duration::from_secs(3)) }, |_| Message::Tick)
                .chain(iced::window::latest().and_then(iced::window::screenshot).map(Message::Shot))
        } else if tab == Tab::Voice || panel.guide_open {
            panel.voice_poll()
        } else if tab == Tab::Assistant {
            panel.speech_poll()
        } else {
            Task::none()
        };
        // the jack, once: on a board built with the AC101 the guide and Controls offer the switch
        let task = if demo || panel.screenshot.is_some() { task } else { Task::batch([task, Task::perform(blocking(sys::jack), Message::Jack)]) };
        (panel, task)
    }

    fn subscription(&self) -> Subscription<Message> {
        Subscription::batch([
            iced::time::every(Duration::from_millis(1000)).map(|_| Message::Tick),
            iced::window::close_requests().map(|_| Message::Close),
        ])
    }

    fn note(&mut self, s: String) {
        // (a progress bar's carriage-returned states as its last one, long lines cut: sys::log_lines)
        for l in sys::log_lines(&s) {
            if self.log.len() == 200 {
                self.log.pop_front();
            }
            self.log.push_back(l);
        }
    }

    fn update(&mut self, message: Message) -> Task<Message> {
        match message {
            Message::Tick => {
                // a screenshot is taken of settled values: the demo's numbers stop moving after two seconds
                if self.screenshot.is_some() && self.ticks >= 2 {
                    return Task::none();
                }
                self.ticks += 1;
                let mut tasks = Vec::new();
                if self.demo {
                    self.t += 1.0;
                    self.snap = sys::demo::snapshot(self.t);
                    self.privileged = sys::demo::privileged(self.t);
                    self.procs = sys::demo::processes(self.t);
                    self.gpu = sys::demo::gpu(self.t);
                    push(&mut self.hist_gpu, self.gpu.load.unwrap_or(0) as f32 / 100.0);
                } else {
                    let now = sys::cpu_times();
                    self.snap = sys::snapshot(&self.prev_cpu, &now);
                    self.prev_cpu = now;
                    if self.ticks % 2 == 1 && !self.priv_pending {
                        self.priv_pending = true;
                        tasks.push(Task::perform(blocking(sys::privileged), Message::Privileged));
                    }
                    // the GPU's own figures need root (a7a-gpu-stat through sudo, kept out of the journal by the
                    // image): every 2 s, only while a tab that shows them is open
                    if self.tab == Tab::Controls && self.ticks % 3 == 0 && !self.wifi_pending {
                        self.wifi_pending = true;
                        tasks.push(Task::perform(blocking(sys::wifi), Message::Wifi));
                    }
                    if matches!(self.tab, Tab::Overview | Tab::Hardware) && self.ticks % 2 == 0 && !self.gpu_pending {
                        self.gpu_pending = true;
                        tasks.push(Task::perform(blocking(sys::gpu_stat), Message::GpuStat));
                    }
                    if (self.tab == Tab::Hardware || self.procs.is_empty()) && self.ticks % 2 == 0 && !self.proc_pending {
                        self.proc_pending = true;
                        let prev = self.proc_ticks.clone();
                        let elapsed = self.proc_at.elapsed().as_secs_f32();
                        self.proc_at = Instant::now();
                        tasks.push(Task::perform(blocking(move || sys::processes(&prev, elapsed)), |(p, t)| Message::Processes(p, t)));
                    }
                }
                let s = &self.snap;
                push(&mut self.hist_big, s.big.cur as f32 / s.big.top.max(1) as f32);
                push(&mut self.hist_little, s.little.cur as f32 / s.little.top.max(1) as f32);
                let hot = s.temps.iter().map(|(_, t)| *t).fold(0.0f32, f32::max);
                push(&mut self.hist_temp, hot / 90.0);
                push(&mut self.hist_mem, 1.0 - s.mem_avail_mb as f32 / s.mem_total_mb.max(1) as f32);
                if self.cap_little.is_none() && s.little.cap > 0 {
                    self.cap_little = Some(s.little.cap);
                    self.cap_big = Some(s.big.cap);
                }
                if let Some(b) = self.privileged.gpu_boot_mhz {
                    if self.ticks < 4 {
                        self.gpu_clock = b;
                    }
                }
                if self.download.is_some() {
                    self.download = Some(agent::download_progress().unwrap_or(self.download.unwrap_or(0.0)));
                }
                // the model server's state, while the Assistant is open: every 2 s, every second while it changes
                if self.tab == Tab::Assistant && self.screenshot.is_none() && !self.llm_pending && (self.ticks % 2 == 0 || self.llm != "ready") {
                    self.llm_pending = true;
                    tasks.push(Task::perform(blocking(agent::health), Message::LlmHealth));
                }
                // answers read aloud, while the Assistant is open: every 2 s, every second while something changes
                if self.tab == Tab::Assistant && self.screenshot.is_none() && (self.ticks % 2 == 0 || self.speech_busy || self.reading > 0 || self.speech.speaking) {
                    tasks.push(self.speech_poll());
                }
                // the listener's state, while the Voice tab is open: every 2 s, every second while something changes
                let setting_up = self.voice.setup.as_ref().is_some_and(|p| p.error.is_empty());
                if (self.tab == Tab::Voice || self.guide_open) && (self.ticks % 2 == 0 || self.voice_busy || setting_up) {
                    tasks.push(self.voice_poll());
                }
                // the guide's model step: a download finished, a model copied in by hand
                if self.guide_open && !self.demo && self.ticks % 3 == 0 {
                    self.models = agent::models();
                }
                Task::batch(tasks)
            }
            Message::Privileged(p) => {
                self.priv_pending = false;
                self.privileged = p;
                Task::none()
            }
            Message::Wifi(w) => {
                self.wifi_pending = false;
                self.wifi = w;
                Task::none()
            }
            Message::GpuStat(g) => {
                self.gpu_pending = false;
                if let Some(l) = g.load {
                    // one point per answer, every 2 s: the line covers two minutes
                    push(&mut self.hist_gpu, l as f32 / 100.0);
                }
                self.gpu = g;
                Task::none()
            }
            Message::Processes(p, t) => {
                self.proc_pending = false;
                self.procs = p;
                self.proc_ticks = t;
                Task::none()
            }
            Message::Tab(t) => {
                self.tab = t;
                self.guide_open = false;
                if t == Tab::Assistant {
                    self.models = agent::models();
                    if self.model.as_ref().is_none_or(|m| !self.models.contains(m)) {
                        self.model = self.models.iter().find(|m| m.starts_with("Qwen3-4B")).or(self.models.first()).cloned();
                    }
                    return self.speech_poll();
                }
                if t == Tab::Voice {
                    return self.voice_poll();
                }
                Task::none()
            }
            Message::Guide(open) => {
                self.guide_open = open;
                self.guide_error.clear();
                if open {
                    self.models = agent::models();
                    return self.voice_poll();
                }
                Task::none()
            }
            Message::GuideAtStart(on) => {
                if self.demo {
                    self.guide_at_start = on;
                    return Task::none();
                }
                match guide::save_at_start(&guide::path(), on) {
                    Ok(()) => {
                        self.guide_at_start = on;
                        self.guide_error.clear();
                    }
                    Err(e) => self.guide_error = format!("not saved: {e}"),
                }
                Task::none()
            }
            Message::GuideGo(t) => self.update(Message::Tab(t)),
            Message::GuideSettings => {
                let t = self.update(Message::Tab(Tab::Assistant));
                let _ = self.update(Message::SettingsOpen);
                t
            }
            Message::GuideAddOnline(provider) => {
                let t = self.update(Message::GuideSettings);
                self.prefs_edit.online.push(prefs::Online { name: guide::name(provider), provider, on: true, ..Default::default() });
                t
            }
            Message::Power(p) => {
                if self.demo {
                    self.note(format!("(demo) power profile -> {p}"));
                    return Task::none();
                }
                if self.busy {
                    self.note("… a command is still running: wait for it".into());
                    return Task::none();
                }
                self.busy = true;
                // through tuned itself, also for the profile already set: that puts back the clocks changed below
                Task::perform(blocking(move || sys::set_power(p)), Message::Done)
            }
            Message::Run(args) => {
                if self.busy {
                    self.note("… a command is still running: wait for it".into());
                    return Task::none();
                }
                self.note(format!("$ a7a-clock {}", args.join(" ")));
                if self.demo {
                    self.note("(demo: nothing was changed)".into());
                    return Task::none();
                }
                self.busy = true;
                Task::perform(blocking(move || sys::a7a_clock(args)), Message::Done)
            }
            Message::Done(out) => {
                self.busy = false;
                self.note(out);
                Task::none()
            }
            Message::Jack(j) => {
                self.jack = j;
                Task::none()
            }
            Message::CodecSet(c) => {
                if self.busy {
                    self.note("… a command is still running: wait for it".into());
                    return Task::none();
                }
                self.note(format!("$ a7a-clock codec {}", c.arg()));
                if self.demo {
                    self.note("(demo: nothing was changed)".into());
                    return Task::none();
                }
                self.busy = true;
                Task::perform(blocking(move || (sys::a7a_clock(vec!["codec".into(), c.arg().into()]), sys::jack())), |(out, j)| Message::CodecDone(out, j))
            }
            Message::CodecDone(out, j) => {
                self.busy = false;
                // still asking for the switch: it did not go through, and the guide has no Output box to say why
                self.jack_error = if j.fix().is_some() {
                    match sys::why_failed(&out) {
                        w if w.is_empty() => "The switch stopped without a word (its output is in Controls).".into(),
                        w => format!("The switch stopped: {w}"),
                    }
                } else {
                    String::new()
                };
                self.note(out);
                self.jack = j;
                Task::none()
            }
            Message::Governor(g) => self.update(Message::Run(vec!["cpu".into(), g])),
            Message::CapLittle(v) => {
                self.cap_little = Some(v);
                Task::none()
            }
            Message::CapBig(v) => {
                self.cap_big = Some(v);
                Task::none()
            }
            Message::ApplyCaps => {
                let (l, b) = (self.cap_little.unwrap_or(0), self.cap_big.unwrap_or(0));
                self.update(Message::Run(vec!["cpumax".into(), l.to_string(), b.to_string()]))
            }
            Message::GpuClock(c) => {
                self.gpu_clock = c;
                Task::none()
            }
            Message::ApplyGpu => self.update(Message::Run(vec!["gpu".into(), self.gpu_clock.to_string()])),
            Message::Shot(shot) => {
                if let Some(path) = &self.screenshot {
                    let _ = std::fs::write(path, &shot.rgba);
                    let _ = std::fs::write(format!("{path}.size"), format!("{}x{}\n", shot.size.width, shot.size.height));
                }
                iced::exit()
            }
            Message::Input(s) => {
                self.input = s;
                Task::none()
            }
            Message::Ask(q) => {
                self.input = q;
                self.update(Message::Send)
            }
            Message::Send => {
                let q = self.input.trim().to_string();
                if q.is_empty() || self.answering || self.waiting.is_some() {
                    return Task::none();
                }
                self.input.clear();
                self.lines.push(Line::User(q.clone()));
                // an answer still being read stops: a new question is under way
                let quiet = self.quiet();
                if self.llm == "ready" || self.prefs.online_named(&self.prefs.ask_with).is_some() {
                    return Task::batch([quiet, self.ask(q)]);
                }
                // a question typed before the model is up is kept, and goes out as soon as the model is loaded
                self.waiting = Some(q);
                if self.llm == "stopped" && !self.llm_busy {
                    self.lines.push(Line::Note("Starting the model server first. Loading the model takes a minute or two; the question goes out as soon as it is ready.".into(), ui::DIM));
                    return Task::batch([quiet, self.update(Message::LlmStart)]);
                }
                self.lines.push(Line::Note("The model is still loading; the question goes out as soon as it is ready.".into(), ui::DIM));
                quiet
            }
            Message::StopAnswer => {
                if self.waiting.take().is_some() {
                    self.lines.push(Line::Note("(not sent)".into(), ui::DIM));
                    return Task::none();
                }
                if !self.answering {
                    return Task::none();
                }
                self.stop.store(true, Ordering::SeqCst);
                self.run += 1;
                self.answering = false;
                let partial = match self.lines.last() {
                    Some(Line::Answer(_, raw)) => raw.trim().to_string(),
                    _ => String::new(),
                };
                if partial.is_empty() {
                    if let Some(Line::Answer(..)) = self.lines.last() {
                        self.lines.pop();
                    }
                    self.history.pop(); // the question, which got no answer
                } else {
                    self.history.push(json!({"role": "assistant", "content": partial}));
                }
                self.lines.push(Line::Note("(stopped)".into(), ui::DIM));
                self.trim_history();
                Task::none()
            }
            Message::NewChat => {
                self.stop.store(true, Ordering::SeqCst);
                self.run += 1;
                self.answering = false;
                self.waiting = None;
                self.lines.clear();
                self.history.truncate(1);
                // a fresh flag: an answer still winding down keeps the old one
                self.taint = Arc::new(agent::Taint::default());
                self.rate = None;
                self.quiet()
            }
            Message::Agent(id, ev) => {
                if id != self.run {
                    return Task::none();
                }
                let mut next = Task::none();
                match ev {
                    agent::Event::Chunk(piece) => {
                        if let Some(Line::Answer(content, raw)) = self.lines.last_mut() {
                            content.push_str(&piece);
                            raw.push_str(&piece);
                        }
                    }
                    agent::Event::Tool { audit, observation } => {
                        // the tool goes above the answer that is still to come
                        let answer = match self.lines.last() {
                            Some(Line::Answer(_, raw)) if raw.is_empty() => self.lines.pop(),
                            _ => None,
                        };
                        self.lines.push(Line::Tool { audit, observation });
                        self.lines.push(answer.unwrap_or_else(|| Line::Answer(markdown::Content::new(), String::new())));
                    }
                    agent::Event::Done { text, tok_s, tokens } => {
                        self.answering = false;
                        self.pool.retain(&self.prefs.mcp); // (one an answer started after a Save took it away)
                        if tok_s > 0.0 {
                            self.rate = Some((tok_s, tokens));
                        }
                        if let Some(Line::Answer(_, raw)) = self.lines.last() {
                            if raw.trim().is_empty() {
                                self.lines.pop();
                            }
                        }
                        let text = text.trim().to_string();
                        if text.is_empty() {
                            self.lines.push(Line::Note("(the model gave no answer)".into(), ui::DIM));
                            self.history.pop();
                        } else {
                            if self.speech_want.unwrap_or(self.speech.speak) && self.speech.ready && !self.demo {
                                self.reading += 1;
                                let said = text.clone();
                                next = Task::perform(blocking(move || voice::say(&said)), Message::Spoken);
                            }
                            self.history.push(json!({"role": "assistant", "content": text}));
                        }
                        self.trim_history();
                    }
                    agent::Event::Note(n) => {
                        // above the answer that is still to come, as a tool is
                        let answer = match self.lines.last() {
                            Some(Line::Answer(_, raw)) if raw.is_empty() => self.lines.pop(),
                            _ => None,
                        };
                        self.lines.push(Line::Note(n, ui::WARN));
                        self.lines.extend(answer);
                    }
                    agent::Event::Fail(e) => {
                        self.answering = false;
                        self.pool.retain(&self.prefs.mcp); // (one an answer started after a Save took it away)
                        if let Some(Line::Answer(_, raw)) = self.lines.last() {
                            if raw.is_empty() {
                                self.lines.pop();
                            }
                        }
                        self.lines.push(Line::Note(e, ui::HOT));
                        // the question got no answer: it leaves the model's memory and goes back into the box
                        if let Some(q) = self.history.pop().and_then(|m| m["content"].as_str().map(str::to_string)) {
                            if self.input.is_empty() {
                                self.input = q;
                            }
                        }
                    }
                }
                next
            }
            Message::LlmHealth(s) => {
                self.llm_pending = false;
                let was = self.llm;
                self.llm = if self.llm_busy && s == "stopped" { "starting" } else { s };
                if s == "ready" {
                    if let Some(q) = self.waiting.take() {
                        return self.ask(q);
                    }
                } else if s == "stopped" && was == "loading" && !self.llm_busy {
                    if let Some(q) = self.waiting.take() {
                        self.lines.push(Line::Note("The model server stopped while it was loading the model (its log: /tmp/a7a-llm.log).".into(), ui::HOT));
                        self.input = q;
                    }
                }
                Task::none()
            }
            Message::LlmStart => {
                if self.llm_busy {
                    return Task::none();
                }
                let Some(model) = self.model.clone() else {
                    self.lines.push(Line::Note(format!("There is no model in {}. Download one with: a7a-llm get", agent::models_dir()), ui::WARN));
                    if let Some(q) = self.waiting.take() {
                        self.input = q;
                    }
                    return Task::none();
                };
                self.llm_busy = true;
                self.llm = "starting";
                Task::perform(blocking(move || sys::command("a7a-llm", &["serve", &model])), Message::LlmDone)
            }
            Message::LlmStop => {
                if self.llm_busy {
                    return Task::none();
                }
                // (an online assistant's answer goes on: it does not need this board's model)
                let board = self.prefs.online_named(&self.prefs.ask_with).is_none();
                let stop = if board && (self.answering || self.waiting.is_some()) { self.update(Message::StopAnswer) } else { Task::none() };
                self.llm_busy = true;
                stop.chain(Task::perform(blocking(|| sys::command("a7a-llm", &["stop"])), Message::LlmDone))
            }
            Message::LlmDone(out) => {
                self.llm_busy = false;
                self.llm = "unknown";
                self.note(out.clone());
                let failed = !["up:", "already running", "stopped", "not running"].iter().any(|k| out.contains(k));
                let shown: Vec<&str> = out.lines().filter(|l| !l.trim().is_empty()).collect();
                let shown = shown[shown.len().saturating_sub(6)..].join("\n");
                self.lines.push(Line::Note(shown, if failed { ui::HOT } else { ui::DIM }));
                if failed {
                    if let Some(q) = self.waiting.take() {
                        self.input = q;
                    }
                }
                Task::none()
            }
            Message::LlmGet => {
                if self.download.is_some() {
                    return Task::none();
                }
                if self.demo {
                    self.lines.push(Line::Note("(demo: nothing downloaded)".into(), ui::DIM));
                    return Task::none();
                }
                self.download = Some(agent::download_progress().unwrap_or(0.0));
                self.lines.push(Line::Note(format!("Downloading {} (2.4 GB) into {}…", agent::DEFAULT_MODEL, agent::models_dir()), ui::DIM));
                Task::perform(blocking(|| sys::command("a7a-llm", &["get"])), Message::LlmGot)
            }
            Message::LlmGot(out) => {
                self.download = None;
                self.note(out.clone());
                self.models = agent::models();
                let ok = self.models.iter().any(|m| m == agent::DEFAULT_MODEL);
                if ok {
                    self.model = Some(agent::DEFAULT_MODEL.into());
                    self.lines.push(Line::Note("The model is here. Press Start, or just ask: the server starts by itself.".into(), ui::GOOD));
                } else {
                    self.lines.push(Line::Note(format!("The download did not finish: {}", sys::last_said(&out).unwrap_or("no answer from a7a-llm")), ui::HOT));
                }
                Task::none()
            }
            Message::Model(m) => {
                if self.llm == "ready" && self.model.as_ref() != Some(&m) {
                    self.lines.push(Line::Note(format!("{m} is used from the next start of the server (Stop, then Start)."), ui::DIM));
                }
                self.model = Some(m);
                Task::none()
            }
            Message::Web(on) => {
                self.web = on;
                if !self.demo {
                    self.prefs.web = on;
                    if !self.prefs_error.is_empty() {
                        // the defaults stand in for a file that does not read: saved, they would take its place, its
                        // servers and folders gone (review 2026-11-19)
                        self.lines.push(Line::Note(format!("Not remembered: the settings file does not read ({}). Fix it, or Save it again from Settings.", self.prefs_error), ui::WARN));
                    } else if let Err(why) = self.prefs.savable() {
                        // folders that could not be used, written back, would read as none: everywhere
                        self.lines.push(Line::Note(format!("Not remembered: {why}. Set the folders in Settings first."), ui::WARN));
                    } else if let Err(e) = self.prefs.save(&prefs::Prefs::path()) {
                        self.lines.push(Line::Note(format!("The setting was not saved: {e}"), ui::HOT));
                    }
                }
                Task::none()
            }
            Message::Link(uri) => {
                // a link shows its text, not where it goes: in a chat that has looked at this board it could carry what
                // the model read (a file can carry instructions as well as a page), so it is shown, not opened
                if self.taint.local.load(Ordering::SeqCst) {
                    self.lines.push(Line::Note(
                        format!("Not opened: this chat has read from the board, and a link could carry what it read. The link goes to: {uri}"),
                        ui::WARN,
                    ));
                } else if uri.starts_with("https://") || uri.starts_with("http://") {
                    let _ = std::process::Command::new("xdg-open").arg(&uri).spawn();
                }
                Task::none()
            }
            Message::Speech(gen, s) => {
                self.speech_pending = false;
                if gen != self.speech_gen {
                    return self.speech_poll(); // asked before a change: asked again
                }
                // while a change is at work, the switch and the voice shown are the ones asked for
                let (speak, voice) = (self.speech.speak, self.speech.voice.clone());
                self.speech = s;
                if self.speech_busy {
                    self.speech.speak = speak;
                    self.speech.voice = voice;
                }
                Task::none()
            }
            Message::Speak(on) => {
                if self.speech_busy {
                    return Task::none();
                }
                self.note(format!("$ a7a-voice speak {}", if on { "on" } else { "off" }));
                if self.demo {
                    self.speech.speak = on;
                    self.note("(demo: nothing was changed)".into());
                    return Task::none();
                }
                if on && !self.speech.ready {
                    self.lines.push(Line::Note("Getting the voice first (about 130 MB, a minute or two); then a sample is read in it.".into(), ui::DIM));
                }
                // a minute or two the first time: the ticks ask for the download's progress every second meanwhile
                self.speech_busy = true;
                self.speech_want = Some(on);
                self.speech_gen += 1;
                Task::perform(blocking(move || voice::speak(on)), Message::SpeakDone)
            }
            Message::SpeakVoice(v) => {
                if self.speech_busy || v.0 == self.speech.voice {
                    return Task::none();
                }
                self.note(format!("$ a7a-voice speak voice {}", v.0));
                self.speech.voice = v.0.clone();
                if self.demo {
                    return Task::none();
                }
                self.speech_busy = true;
                self.speech_gen += 1;
                Task::perform(blocking(move || voice::speak_voice(&v.0)), Message::SpeakDone)
            }
            Message::SpeakDone(out) => {
                self.speech_busy = false;
                self.speech_gen += 1;
                self.note(out.clone());
                let failed = out.contains("\n(exit ") || out.contains("could not be started");
                if failed {
                    let why = out.lines().filter(|l| !l.trim().is_empty() && !l.starts_with("(exit ")).last().unwrap_or("a7a-voice failed");
                    self.lines.push(Line::Note(format!("Speak answers: {why}"), ui::HOT));
                } else if let Some(on) = self.speech_want {
                    // done: shown as asked until the next answer from a7a-voice says so too
                    self.speech.speak = on;
                    self.speech.ready |= on;
                }
                self.speech_want = None;
                self.speech_poll()
            }
            Message::Hush => {
                if self.demo {
                    return Task::none();
                }
                Task::perform(blocking(voice::hush), Message::Hushed)
            }
            Message::Hushed(out) => {
                self.note(out);
                self.speech_poll()
            }
            Message::Close => {
                // the MCP servers stop before the panel goes (threads left to stop them would go with it: a server
                // that does not end on its stdin outlived the panel, review), and a reading the tab started (not one
                // of "hey assistant"'s): a second or two at most
                let (pool, reading) = (self.pool.clone(), self.reading > 0 && !self.demo);
                Task::perform(
                    blocking(move || {
                        pool.stop_all(true);
                        if reading {
                            voice::hush();
                        }
                    }),
                    |_| Message::Exit,
                )
            }
            Message::Exit => iced::exit(),
            Message::SettingsOpen => {
                self.prefs_edit = self.prefs.clone();
                self.instructions = text_editor::Content::with_text(&self.prefs.instructions);
                self.folder_input.clear();
                self.settings_error.clear();
                self.online_untested(None);
                self.settings_open = true;
                Task::none()
            }
            Message::SettingsCancel => {
                self.settings_open = false;
                // a server started for "Its tools" and not saved stops
                self.pool.retain(&self.prefs.mcp);
                Task::none()
            }
            Message::SettingsSave => {
                // the folders as the page lists them (what the file had that could not be used is left behind); a
                // command line or an environment that does not read is said now, not at the first question
                let edited = prefs::Prefs { ask_with: self.prefs.ask_with.clone(), ..self.prefs_edit.clone() };
                let p = match edited.for_save(self.instructions.text().trim_end().to_string(), self.web) {
                    Ok(p) => p,
                    Err(e) => {
                        self.settings_error = format!("Not saved: {e}");
                        return Task::none();
                    }
                };
                if self.demo {
                    self.prefs = p;
                    self.settings_open = false;
                    self.note("(demo: nothing saved)".into());
                    return Task::none();
                }
                let mut next = Task::none();
                match p.save(&prefs::Prefs::path()) {
                    Ok(()) => {
                        let context = p.context != self.prefs.context;
                        // the one that answers gone, switched off, or another provider or model: a new chat, as
                        // choosing under "ask" starts one (nothing one has read goes on to another)
                        let answerer = prefs::Prefs::answerer_changed(self.prefs.online_named(&self.prefs.ask_with), p.online_named(&p.ask_with));
                        self.prefs = p;
                        self.prefs_error.clear();
                        self.settings_error.clear();
                        self.settings_open = false;
                        // servers removed, changed or switched off stop now; the others are tried again
                        self.pool.retain(&self.prefs.mcp);
                        self.pool.forget_all();
                        self.trim_history();
                        if answerer {
                            next = self.update(Message::NewChat);
                            self.lines.push(Line::Note("A new chat: the assistant that answers here has changed.".into(), ui::DIM));
                        }
                        if context && matches!(self.llm, "ready" | "loading") {
                            self.lines.push(Line::Note(format!("The model server uses a context of {} tokens from its next start: Stop server, then Start.", self.prefs.context), ui::DIM));
                        }
                    }
                    Err(e) => self.settings_error = format!("Not saved: {e}"),
                }
                next
            }
            Message::Instructions(action) => {
                self.instructions.perform(action);
                Task::none()
            }
            Message::FolderInput(f) => {
                self.folder_input = f;
                Task::none()
            }
            Message::FolderAdd => {
                let f = self.folder_input.trim().to_string();
                if f.is_empty() {
                    return Task::none();
                }
                if !(f.starts_with('/') || f == "~" || f.starts_with("~/")) {
                    self.settings_error = "A folder is a full path (/home/radxa/Documents) or one under ~ (~/Documents).".into();
                    return Task::none();
                }
                self.prefs_edit.folders.push(f);
                // the folders only: a server being typed in must not be cleaned away under the owner's hands
                self.prefs_edit.folders = prefs::clean_folders(&self.prefs_edit.folders);
                self.folder_input.clear();
                self.settings_error.clear();
                Task::none()
            }
            Message::FolderRemove(i) => {
                if i < self.prefs_edit.folders.len() {
                    self.prefs_edit.folders.remove(i);
                }
                Task::none()
            }
            Message::WebSearch(on) => {
                self.prefs_edit.web_search = on;
                Task::none()
            }
            Message::FetchUrl(on) => {
                self.prefs_edit.fetch_url = on;
                Task::none()
            }
            Message::Weather(on) => {
                self.prefs_edit.weather = on;
                Task::none()
            }
            Message::Memory(n) => {
                self.prefs_edit.memory = n;
                Task::none()
            }
            Message::AnswerLength(n) => {
                self.prefs_edit.answer_length = n;
                Task::none()
            }
            Message::Context(n) => {
                self.prefs_edit.context = n;
                Task::none()
            }
            Message::McpAdd => {
                self.prefs_edit.mcp.push(prefs::Server { on: true, ..Default::default() });
                Task::none()
            }
            Message::McpRemove(i) => {
                if i < self.prefs_edit.mcp.len() {
                    self.prefs_edit.mcp.remove(i);
                }
                Task::none()
            }
            Message::McpName(i, v) => {
                if let Some(sv) = self.prefs_edit.mcp.get_mut(i) {
                    sv.name = v;
                }
                Task::none()
            }
            Message::McpCommand(i, v) => {
                if let Some(sv) = self.prefs_edit.mcp.get_mut(i) {
                    sv.command = v;
                }
                Task::none()
            }
            Message::McpEnv(i, v) => {
                if let Some(sv) = self.prefs_edit.mcp.get_mut(i) {
                    sv.env = v;
                }
                Task::none()
            }
            Message::McpReach(i, r) => {
                if let Some(sv) = self.prefs_edit.mcp.get_mut(i) {
                    sv.reach = r;
                }
                Task::none()
            }
            Message::McpOn(i, on) => {
                if let Some(sv) = self.prefs_edit.mcp.get_mut(i) {
                    sv.on = on;
                }
                Task::none()
            }
            Message::McpLoad(i) => {
                // the server as it would be saved (its name a tool-name prefix), started now (anew, if it failed
                // lately; with a first start's time: it may download) to ask for its tools
                let Some(sv) = self.prefs_edit.mcp.get(i).and_then(prefs::as_saved) else {
                    self.settings_error = "Its command first.".into();
                    return Task::none();
                };
                let key = mcp_key(&sv);
                if self.demo || self.mcp_loading.contains(&key) {
                    return Task::none();
                }
                self.mcp_loading.insert(key.clone());
                let pool = self.pool.clone();
                Task::perform(blocking(move || {
                    pool.forget(&sv);
                    pool.get(&sv, mcp::FIRST_START).map(|c| c.lock().unwrap().tools.clone())
                }), move |r| Message::McpLoaded(key.clone(), r))
            }
            Message::McpLoaded(key, r) => {
                self.mcp_loading.remove(&key);
                self.mcp_found.insert(key, r);
                if !self.settings_open {
                    // it came after Save or Cancel: what the settings do not have stops
                    self.pool.retain(&self.prefs.mcp);
                }
                Task::none()
            }
            Message::AskWith(choice) => {
                let name = if choice == BOARD_MODEL { String::new() } else { choice };
                if name == self.prefs.ask_with {
                    return Task::none();
                }
                // a new chat: what the board's model read must not go to a provider, nor a provider's chat to it
                let quiet = self.update(Message::NewChat);
                self.prefs.ask_with = name;
                if !self.demo && self.prefs_error.is_empty() && self.prefs.savable().is_ok() {
                    if let Err(e) = self.prefs.save(&prefs::Prefs::path()) {
                        self.lines.push(Line::Note(format!("The choice was not saved: {e}"), ui::HOT));
                    }
                }
                quiet
            }
            Message::OnlineAdd => {
                let n = self.prefs_edit.online.len();
                let provider = prefs::Provider::ALL[n.min(1)]; // Google first, then Cloudflare
                self.prefs_edit.online.push(prefs::Online { name: provider.key().into(), provider, on: true, ..Default::default() });
                Task::none()
            }
            Message::OnlineRemove(i) => {
                if i < self.prefs_edit.online.len() {
                    self.prefs_edit.online.remove(i);
                    self.online_untested(None); // the rows after it move up
                }
                Task::none()
            }
            Message::OnlineName(i, v) => {
                if let Some(o) = self.prefs_edit.online.get_mut(i) {
                    o.name = v;
                }
                self.online_untested(Some(i));
                Task::none()
            }
            Message::OnlineProvider(i, pr) => {
                if let Some(o) = self.prefs_edit.online.get_mut(i) {
                    // a name still the old provider's follows the new one
                    if o.name.trim().is_empty() || o.name.trim() == o.provider.key() {
                        o.name = pr.key().into();
                    }
                    o.provider = pr;
                    o.model.clear();
                }
                self.online_untested(Some(i));
                Task::none()
            }
            Message::OnlineModel(i, v) => {
                if let Some(o) = self.prefs_edit.online.get_mut(i) {
                    o.model = v;
                }
                self.online_untested(Some(i));
                Task::none()
            }
            Message::OnlineUrl(i, v) => {
                if let Some(o) = self.prefs_edit.online.get_mut(i) {
                    o.url = v;
                }
                self.online_untested(Some(i));
                Task::none()
            }
            Message::OnlineKey(i, v) => {
                if let Some(o) = self.prefs_edit.online.get_mut(i) {
                    o.key = v;
                }
                self.online_untested(Some(i));
                Task::none()
            }
            Message::OnlineOn(i, on) => {
                if let Some(o) = self.prefs_edit.online.get_mut(i) {
                    o.on = on;
                }
                Task::none()
            }
            Message::OnlineTest(i) => {
                let Some(o) = self.prefs_edit.online.get(i).cloned() else { return Task::none() };
                if self.demo || self.online_testing.contains_key(&i) {
                    return Task::none();
                }
                // as it would be saved (its name, its key trimmed)
                let o = prefs::Prefs { online: vec![o], ..Default::default() }.cleaned().online.remove(0);
                self.online_tests += 1;
                let test = self.online_tests;
                self.online_testing.insert(i, test);
                self.online_tested.remove(&i);
                Task::perform(blocking(move || online::test(&o)), move |r| Message::OnlineTested(i, test, r))
            }
            Message::OnlineTested(i, test, r) => {
                // not for a row changed (or moved up) since it was asked
                if self.online_testing.get(&i) == Some(&test) {
                    self.online_testing.remove(&i);
                    self.online_tested.insert(i, r);
                }
                Task::none()
            }
            Message::McpTool(i, name, on) => {
                if let Some(sv) = self.prefs_edit.mcp.get_mut(i) {
                    sv.off_tools.retain(|t| t != &name);
                    if !on {
                        sv.off_tools.push(name);
                    }
                }
                Task::none()
            }
            Message::Spoken(out) => {
                self.reading = self.reading.saturating_sub(1);
                self.note(out.clone());
                if let Some(why) = voice::unread(&out) {
                    self.lines.push(Line::Note(why.to_string(), ui::WARN));
                }
                self.speech_poll()
            }
            Message::VoiceStatus(gen, s) => {
                self.voice_pending = false;
                self.voice_known = true;
                // what is typed and not saved stays, and so does what is being saved; otherwise the tab shows the
                // settings the listener has (as it cleaned them up: a wake phrase's spaces, numbers in range). A
                // settings file that does not read comes with no commands: the tab keeps the ones it showed, so a
                // Save writes them back rather than nothing
                let had = self.voice_known_config;
                if !self.voice_dirty && !self.voice_busy && gen == self.voice_gen && (s.config_error.is_empty() || !had) {
                    self.voice_edit = s.config.clone();
                    self.voice_known_config = s.config_error.is_empty();
                }
                self.voice = s;
                Task::none()
            }
            Message::VoiceWake(w) => {
                self.voice_edit.wake = w;
                self.voice_dirty = true;
                Task::none()
            }
            Message::VoiceAssistant(w) => {
                self.voice_edit.assistant = w;
                self.voice_dirty = true;
                Task::none()
            }
            Message::VoiceSay(i, s) => {
                if let Some(c) = self.voice_edit.commands.get_mut(i) {
                    c.say = s;
                    self.voice_dirty = true;
                }
                Task::none()
            }
            Message::VoiceRun(i, r) => {
                if let Some(c) = self.voice_edit.commands.get_mut(i) {
                    c.run = r;
                    self.voice_dirty = true;
                }
                Task::none()
            }
            Message::VoiceAdd => {
                self.voice_edit.commands.push(voice::Command::default());
                self.voice_dirty = true;
                Task::none()
            }
            Message::VoiceRemove(i) => {
                if i < self.voice_edit.commands.len() {
                    self.voice_edit.commands.remove(i);
                    self.voice_dirty = true;
                }
                Task::none()
            }
            Message::VoiceSave => {
                if self.voice_busy {
                    return Task::none();
                }
                if self.demo {
                    self.note("(demo: nothing saved)".into());
                    self.voice_dirty = false;
                    return Task::none();
                }
                self.voice_busy = true;
                self.voice_dirty = false;
                self.voice_gen += 1;
                let cfg = self.voice_edit.clone();
                Task::perform(blocking(move || voice::save(&cfg)), Message::VoiceSaved)
            }
            Message::VoiceSaved(out) => {
                self.voice_busy = false;
                self.voice_gen += 1; // a status asked while it saved may carry the settings from before it
                if !voice::saved(&out) {
                    self.voice_dirty = true;
                }
                self.note(out);
                self.voice_poll()
            }
            Message::VoiceFoundSay(i, s) => {
                if let Some(f) = self.voice_edit.found.get_mut(i) {
                    f.say = s;
                    self.voice_dirty = true;
                }
                Task::none()
            }
            Message::VoiceFoundOn(i, on) => {
                if let Some(f) = self.voice_edit.found.get_mut(i) {
                    f.on = on;
                    self.voice_dirty = true;
                }
                Task::none()
            }
            Message::VoiceShowApps(show) => {
                self.voice_show_apps = show;
                Task::none()
            }
            Message::VoiceScan => {
                if self.voice_busy {
                    return Task::none();
                }
                self.note("$ a7a-voice scan".into());
                if self.demo {
                    self.note("(demo: nothing scanned)".into());
                    return Task::none();
                }
                // the scan works on the saved settings: what is typed is saved first; the editor then shows the scan's
                let cfg = self.voice_dirty.then(|| self.voice_edit.clone());
                self.voice_gen += 1;
                self.voice_busy = true;
                self.voice_dirty = false;
                Task::perform(
                    blocking(move || {
                        let saved = cfg.map(|c| voice::save(&c));
                        // a save that failed: no scan over the settings from before it
                        let out = match &saved {
                            Some(s) if !voice::saved(s) => "not scanned: the settings were not saved".to_string(),
                            _ => voice::scan(),
                        };
                        (saved, out)
                    }),
                    |(saved, out)| Message::VoiceTested(saved, out),
                )
            }
            Message::VoiceFoundTest(i) => {
                let Some(say) = self.voice_edit.found.get(i).map(|f| f.say.trim().to_string()) else { return Task::none() };
                self.voice_test(say)
            }
            Message::VoiceTest(i) => {
                let Some(say) = self.voice_edit.commands.get(i).map(|c| c.say.trim().to_string()) else { return Task::none() };
                self.voice_test(say)
            }
            Message::VoiceTested(saved, out) => {
                self.voice_busy = false;
                self.voice_gen += 1; // a status asked meanwhile may carry the settings from before a save or a scan
                if let Some(s) = saved {
                    if !voice::saved(&s) {
                        self.voice_dirty = true;
                    }
                    self.note(s);
                }
                self.note(out);
                self.voice_poll()
            }
            Message::VoiceOn(on) => {
                if self.voice_busy {
                    return Task::none();
                }
                self.note(format!("$ a7a-voice {}", if on { "on" } else { "off" }));
                if self.demo {
                    self.note("(demo: nothing was changed)".into());
                    return Task::none();
                }
                self.voice_busy = true;
                Task::perform(blocking(move || if on { voice::on() } else { voice::off() }), Message::VoiceDone)
            }
            Message::VoiceSetup => {
                if self.voice_busy {
                    return Task::none();
                }
                self.note("$ a7a-voice setup".into());
                if self.demo {
                    self.note("(demo: nothing downloaded)".into());
                    return Task::none();
                }
                // a minute or two: the ticks ask for the progress every second meanwhile (voice_busy)
                self.voice_busy = true;
                Task::perform(blocking(voice::setup), Message::VoiceDone)
            }
            Message::VoiceDone(out) => {
                self.voice_busy = false;
                self.note(out);
                self.voice_poll()
            }
        }
    }

    /// Runs the command `say` matches in the saved settings (saving what is typed first): a Test button (one that
    /// presses keys or acts on the window is only said: voice::test).
    fn voice_test(&mut self, say: String) -> Task<Message> {
        if self.voice_busy || say.is_empty() {
            return Task::none();
        }
        self.note(format!("$ a7a-voice test --panel \"{say}\""));
        if self.demo {
            self.note("(demo: nothing was run)".into());
            return Task::none();
        }
        // the test matches against the saved settings: what is typed is saved first
        let cfg = self.voice_dirty.then(|| self.voice_edit.clone());
        if cfg.is_some() {
            self.voice_gen += 1;
        }
        self.voice_busy = true;
        self.voice_dirty = false;
        Task::perform(blocking(move || (cfg.map(|c| voice::save(&c)), voice::test(&say))), |(saved, out)| Message::VoiceTested(saved, out))
    }

    /// Asks whether answers are read aloud (and how far the voice's download is), unless a question is out already.
    fn speech_poll(&mut self) -> Task<Message> {
        if self.demo || self.speech_pending {
            return Task::none();
        }
        self.speech_pending = true;
        let gen = self.speech_gen;
        Task::perform(blocking(voice::speech), move |s| Message::Speech(gen, s))
    }

    /// Stops what is being read aloud, when something is: a new question, a new chat.
    fn quiet(&mut self) -> Task<Message> {
        if self.demo || !(self.reading > 0 || self.speech.speaking) {
            return Task::none();
        }
        Task::perform(blocking(voice::hush), Message::Hushed)
    }

    /// Asks the listener for its state, unless a question is out already (or there is no listener: the demo).
    fn voice_poll(&mut self) -> Task<Message> {
        if self.demo || self.voice_pending {
            return Task::none();
        }
        self.voice_pending = true;
        let gen = self.voice_gen;
        Task::perform(blocking(voice::status), move |s| Message::VoiceStatus(gen, s))
    }

    /// Sends `q` (already in the transcript) to the model, on a thread of its own; the answer streams back as
    /// `Message::Agent` events.
    fn ask(&mut self, q: String) -> Task<Message> {
        self.history.push(json!({"role": "user", "content": q}));
        self.lines.push(Line::Answer(markdown::Content::new(), String::new()));
        self.answering = true;
        self.asked_at = Instant::now();
        self.run += 1;
        let id = self.run;
        let stop = Arc::new(AtomicBool::new(false));
        self.stop = stop.clone();
        let history = self.history.clone();
        if let Some(o) = self.prefs.online_named(&self.prefs.ask_with).cloned() {
            // an online assistant: the chat as it is (its own: choosing it began a new one), its own first words
            let mut msgs = history;
            msgs[0] = json!({"role": "system", "content": online::system_prompt(&o)});
            let (tx, rx) = iced::futures::channel::mpsc::unbounded();
            std::thread::spawn(move || {
                online::ask(&o, &msgs, &stop, &move |ev| {
                    let _ = tx.unbounded_send(ev);
                })
            });
            return Task::run(rx, move |ev| Message::Agent(id, ev));
        }
        let tools = agent::Tools::new(self.taint.clone(), agent::Settings::from_prefs(&self.prefs, self.web), self.pool.clone());
        let (tx, rx) = iced::futures::channel::mpsc::unbounded();
        std::thread::spawn(move || {
            agent::run(history, tools, stop, move |ev| {
                let _ = tx.unbounded_send(ev);
            })
        });
        Task::run(rx, move |ev| Message::Agent(id, ev))
    }

    /// The messages the model sees again with each question, after the system prompt: the last exchanges, as many as
    /// the settings say (three by default). Every one of them is read again before an answer, at about 17 tokens a
    /// second on this board.
    fn trim_history(&mut self) {
        while self.history.len() > 1 + 2 * self.prefs.memory as usize {
            self.history.drain(1..3);
        }
    }

    fn demo_chat(&mut self) {
        self.llm = "ready";
        self.models = vec!["Qwen3-4B-Instruct-2507-Q4_0.gguf".into()];
        self.model = self.models.first().cloned();
        self.rate = Some((3.1, 74));
        self.lines.push(Line::User("How hot is the board right now?".into()));
        self.lines.push(Line::Tool {
            audit: "system_info temp".into(),
            observation: "cpul_thermal_zone: 47C\ncpub_thermal_zone: 49C\ngpu_thermal_zone: 45C\nddr_thermal_zone: 44C".into(),
        });
        let a = "The board is running cool:\n\n- **Big cores (A76):** 49 °C\n- **Little cores (A55):** 47 °C\n- **GPU:** 45 °C\n- **Memory:** 44 °C\n\nAll well under 70 °C, where the panel turns amber.";
        self.lines.push(Line::Answer(markdown::Content::parse(a), a.into()));
        self.lines.push(Line::User("What is using the most memory?".into()));
        self.lines.push(Line::Tool {
            audit: "system_info procs".into(),
            observation: "    PID %CPU %MEM COMMAND\n   1432  4.1  6.2 plasmashell\n   1201  2.3  3.0 kwin_wayland\n   2210  1.0  2.4 firefox".into(),
        });
        let a = "`plasmashell` (the desktop itself) uses the most: about **6 %** of the memory. Then `kwin_wayland`, the window manager, at 3 %.";
        self.lines.push(Line::Answer(markdown::Content::parse(a), a.into()));
    }

    // ---------------------------------------------------------------- view

    fn view(&self) -> Element<'_, Message> {
        let s = &self.snap;
        let hot = s.temps.iter().map(|(_, t)| *t).fold(0.0f32, f32::max);
        let up = s.uptime_s;
        let header = container(
            row![
                column![
                    text("Radxa Cubie A7A").size(22).font(ui::BOLD),
                    text(format!("Fedora 44 · {} · kernel {} · up {}h {:02}m", self.image.variant, self.image.kernel, up / 3600, up / 60 % 60))
                        .size(12)
                        .color(ui::DIM),
                ]
                .spacing(2),
                space::horizontal(),
                chip(s.power.clone(), power_color(&s.power)),
                chip(format!("{hot:.0} °C"), ui::temp_color(hot)),
                chip(
                    format!("GPU {} MHz", self.privileged.gpu_mhz.map(|v| v.to_string()).unwrap_or("—".into())),
                    ui::GPU
                ),
            ]
            .spacing(10)
            .align_y(Alignment::Center),
        )
        .padding([12, 18])
        .width(Fill)
        .style(ui::header);

        // while the guide is open no tab is lit (Tab::Overview never matches a guide that is open: `on`)
        let on = |t: Tab| if self.guide_open { None } else { Some(t) };
        let tabs = row![
            tab_button("Overview", Tab::Overview, on(self.tab)),
            tab_button("Controls", Tab::Controls, on(self.tab)),
            tab_button("Hardware", Tab::Hardware, on(self.tab)),
            tab_button("Assistant", Tab::Assistant, on(self.tab)),
            tab_button("Voice", Tab::Voice, on(self.tab)),
            button(text("Getting started").size(14).font(ui::SEMIBOLD)).padding([7, 16]).style(ui::tab(self.guide_open)).on_press(Message::Guide(!self.guide_open)),
            space::horizontal(),
            text(if self.demo { "demo values" } else { "live · 1 s" }).size(12).color(ui::DIM),
        ]
        .spacing(6)
        .align_y(Alignment::Center);

        let body: Element<'_, Message> = if self.guide_open {
            scrollable(container(self.guide_view()).padding([4, 18])).height(Fill).into()
        } else { match self.tab {
            // the Assistant scrolls its transcript itself, with the question box held below it
            Tab::Assistant => container(self.assistant()).padding(iced::Padding { top: 4.0, right: 18.0, bottom: 12.0, left: 18.0 }).height(Fill).into(),
            t => scrollable(
                container(match t {
                    Tab::Controls => self.controls(),
                    Tab::Hardware => self.hardware(),
                    Tab::Voice => self.voice_tab(),
                    _ => self.overview(),
                })
                .padding([4, 18]),
            )
            .height(Fill)
            .into(),
        } };
        let last = self.log.back().cloned().unwrap_or_else(|| "ready".into());
        let mut page = column![header, container(tabs).padding([10, 18]), body];
        if self.tab != Tab::Assistant || self.guide_open {
            page = page.push(container(text(last).size(12).color(ui::DIM)).padding([6, 18]));
        }
        page.into()
    }

    fn overview(&self) -> Element<'_, Message> {
        let s = &self.snap;
        let p = &self.privileged;
        let big = card(
            "CPU · 2 × Cortex-A76",
            ui::BIG,
            format!("{} MHz", s.big.cur),
            format!("up to {} · {} · busy {:.0}%", s.big.cap, s.big.governor, s.big.load * 100.0),
            Some(spark(&self.hist_big, ui::BIG)),
        );
        let little = card(
            "CPU · 6 × Cortex-A55",
            ui::LITTLE,
            format!("{} MHz", s.little.cur),
            format!("up to {} · {} · busy {:.0}%", s.little.cap, s.little.governor, s.little.load * 100.0),
            Some(spark(&self.hist_little, ui::LITTLE)),
        );
        let g = &self.gpu;
        let gpu_sub = match g.load {
            Some(l) => format!(
                "busy {l}% · {} MB · {} mV, fixed by the boot entry",
                g.mem_mb.map(|v| v.to_string()).unwrap_or("—".into()),
                p.gpu_mv.map(|v| v.to_string()).unwrap_or("—".into())
            ),
            None => format!("{} mV · fixed, set by the boot entry", p.gpu_mv.map(|v| v.to_string()).unwrap_or("—".into())),
        };
        let gpu = card(
            "GPU · PowerVR BXM-4-64",
            ui::GPU,
            format!("{} MHz", p.gpu_mhz.map(|v| v.to_string()).unwrap_or("—".into())),
            gpu_sub,
            g.load.map(|_| spark(&self.hist_gpu, ui::GPU)),
        );
        let mem_used = s.mem_total_mb.saturating_sub(s.mem_avail_mb);
        let ram = card(
            "Memory · LPDDR5",
            ui::RAM,
            format!("{} MHz", s.ram.cur),
            format!("{:.1} of {:.1} GB in use", mem_used as f32 / 1024.0, s.mem_total_mb as f32 / 1024.0),
            Some(spark(&self.hist_mem, ui::RAM)),
        );
        let npu = card("NPU · 3 TOPS", ui::NPU, format!("{} MHz", s.npu.cur), format!("rates {}", list(&s.npu.rates)), None);
        let dsu = card(
            "L3 cache · DSU",
            ui::DSU,
            match (p.dsu_mhz, p.dsu_module.as_str(), p.dsu_rung_mhz) {
                (Some(v), _, _) => format!("{v} MHz"),
                (None, "dsu_dyn", Some(r)) => format!("with the load + {r}"),
                (None, "dsu_dyn", None) => "with the load".into(),
                _ => "—".into(),
            },
            if p.dsu_module.is_empty() || p.dsu_module == "none" { "stock (no module)".into() } else { format!("module {}", p.dsu_module) },
            None,
        );
        let temps = self.temps_card();
        let power = self.power_card();
        column![
            row![big, little].spacing(14),
            row![gpu, ram].spacing(14),
            row![npu, dsu].spacing(14),
            row![temps, power].spacing(14),
        ]
        .spacing(14)
        .into()
    }

    fn temps_card(&self) -> Element<'_, Message> {
        let mut rows = Column::new().spacing(7);
        for (name, t) in &self.snap.temps {
            rows = rows.push(
                row![
                    text(name.clone()).size(13).color(ui::DIM).width(64),
                    canvas(ui::Bar { value: (t - 25.0) / 65.0, color: ui::temp_color(*t) }).width(Fill).height(10),
                    text(format!("{t:.0}°C")).size(13).width(48).align_x(iced::alignment::Horizontal::Right),
                ]
                .spacing(10)
                .align_y(Alignment::Center),
            );
        }
        rows = rows.push(spark(&self.hist_temp, ui::temp_color(self.hist_temp.back().copied().unwrap_or(0.0) * 90.0)));
        boxed(column![label("Temperatures", ui::WARN), rows].spacing(10))
    }

    fn power_card(&self) -> Element<'_, Message> {
        let cur = self.snap.power.as_str();
        let b = |key: &'static str, color: Color| {
            let name = sys::power_name(key);
            button(text(name).size(14).font(ui::SEMIBOLD).align_x(iced::alignment::Horizontal::Center).width(Fill))
                .padding([10, 12])
                .width(Fill)
                .style(ui::pill(color, cur == name))
                .on_press(Message::Power(key))
        };
        boxed(
            column![
                label("Power profile", ui::GOOD),
                text("The same setting as the power slider in the system tray.").size(12).color(ui::DIM),
                row![b("power-saver", ui::DSU), b("balanced", ui::GOOD), b("performance", ui::HOT)].spacing(8),
                text(match cur {
                    "Power Save" => "Little cores up to 1196 MHz, big up to 1508, memory 854 MHz.",
                    "Performance" => "All clocks held at the top, memory at full speed.",
                    _ => "Clocks follow the load up to 2800 / 3000 MHz, memory at full speed.",
                })
                .size(12)
                .color(ui::DIM),
            ]
            .spacing(10),
        )
    }

    fn controls(&self) -> Element<'_, Message> {
        let s = &self.snap;
        let run = |label: String, args: Vec<&str>, accent: Color, selected: bool| -> Element<'_, Message> {
            button(text(label).size(13))
                .padding([7, 14])
                .style(ui::pill(accent, selected))
                .on_press(Message::Run(args.into_iter().map(str::to_string).collect()))
                .into()
        };
        let rates = |dev: &str, r: &sys::Rate, accent: Color| -> Element<'_, Message> {
            let mut rw = row![].spacing(8);
            for v in &r.rates {
                rw = rw.push(run(format!("{v} MHz"), vec![dev, &v.to_string()], accent, *v == r.cur));
            }
            rw.into()
        };
        let dsu = &self.privileged.dsu_module;
        let rung = self.privileged.dsu_rung_mhz;
        let gov = pick_list(s.big.governors.clone(), Some(s.big.governor.clone()), Message::Governor).text_size(13);
        let caps = row![
            text("little up to").size(13).color(ui::DIM),
            pick_list(s.little.steps.clone(), self.cap_little, Message::CapLittle).text_size(13),
            text("big up to").size(13).color(ui::DIM),
            pick_list(s.big.steps.clone(), self.cap_big, Message::CapBig).text_size(13),
            button(text("Apply").size(13)).padding([7, 14]).style(ui::pill(ui::BIG, false)).on_press(Message::ApplyCaps),
            run("Lift the caps".into(), vec!["cpumax", "max", "max"], ui::BIG, false),
        ]
        .spacing(10)
        .align_y(Alignment::Center);
        let gpu = row![
            pick_list(&GPU_CLOCKS[..], Some(self.gpu_clock), Message::GpuClock).text_size(13),
            text("MHz").size(13).color(ui::DIM),
            button(text("Apply at the next start").size(13)).padding([7, 14]).style(ui::pill(ui::GPU, false)).on_press(Message::ApplyGpu),
            run("5-minute stress test".into(), vec!["soak", "5"], ui::GPU, false),
        ]
        .spacing(10)
        .align_y(Alignment::Center);
        column![
            section("Power profile", "The tray's slider. Pressed here, a profile (also the one already set) puts back its CPU, memory and L3 clocks; the NPU, GPU and WiFi settings below stay.", self.power_row()),
            section("CPU", "Governor and the highest clock each cluster may take.", column![row![text("governor").size(13).color(ui::DIM), gov].spacing(10).align_y(Alignment::Center), caps].spacing(10).into()),
            section("Memory clock", "The rates the boot firmware trained at power-on.", rates("ram", &s.ram, ui::RAM)),
            section("NPU", "Its working rate (the module clock itself stays at 1008).", rates("npu", &s.npu, ui::NPU)),
            section(
                "L3 cache (DSU)",
                "Stock 780 MHz, or a module that raises it: with the load (to the bin max, or with the validated 1352 on top), or fixed at 1352.",
                row![
                    run("stock 780".into(), vec!["dsu", "stock"], ui::DSU, dsu.is_empty() || dsu == "none"),
                    run("with the load".into(), vec!["dsu", "dyn"], ui::DSU, dsu == "dsu_dyn" && rung.is_none()),
                    run("with the load + 1352".into(), vec!["dsu", "dynoc"], ui::DSU, dsu == "dsu_dyn" && rung == Some(1352)),
                    run("fixed 1352".into(), vec!["dsu", "1352"], ui::DSU, dsu == "dsu_oc" && self.privileged.dsu_mhz == Some(1352)),
                ]
                .spacing(8)
                .into()
            ),
            section("GPU clock", "A boot entry with the clock and its voltage; the board starts with it after a restart.", gpu.into()),
            section(
                "Presets",
                "The clock panel's four presets (CPU caps, memory, L3 together).",
                row![
                    run("eco".into(), vec!["profile", "eco"], ui::DSU, false),
                    run("balanced".into(), vec!["profile", "balanced"], ui::GOOD, false),
                    run("oc".into(), vec!["profile", "oc"], ui::HOT, false),
                    run("dynoc".into(), vec!["profile", "dynoc"], ui::WARN, false),
                ]
                .spacing(8)
                .into()
            ),
            section(
                "WiFi band",
                "Locks the saved network to a band and connects on it; puts the old band back when the board cannot connect on the new one.",
                {
                    let w = &self.wifi;
                    let lock = w.lock.as_deref();
                    let now = match (w.place(), w.signal_dbm) {
                        (Some(p), Some(s)) => format!(
                            "now on {p} · {s} dBm{}",
                            w.rate_mbit.map(|r| format!(" · {r:.0} Mbit/s")).unwrap_or_default()
                        ),
                        (Some(p), None) => format!("now on {p}"),
                        _ => "not connected".into(),
                    };
                    column![
                        row![
                            run("2.4 GHz".into(), vec!["wifi", "2.4"], ui::LITTLE, lock == Some("bg")),
                            run("5 GHz".into(), vec!["wifi", "5"], ui::LITTLE, lock == Some("a")),
                            run("both".into(), vec!["wifi", "both"], ui::LITTLE, lock == Some("")),
                        ]
                        .spacing(8),
                        text(now).size(12).color(if w.freq_mhz.is_some() { ui::DIM } else { ui::WARN }),
                    ]
                    .spacing(8)
                    .into()
                }
            ),
            section(
                "Headphone jack",
                "Radxa builds the board with one of two sound chips, the AC101 or the AC101B; the stock device tree describes the AC101B only. The switch rewrites the board's device trees (a7a-clock codec) and keeps them so after kernel updates.",
                self.jack_view(),
            ),
            self.log_box(),
        ]
        .spacing(14)
        .into()
    }

    fn power_row(&self) -> Element<'_, Message> {
        let cur = self.snap.power.as_str();
        let b = |key: &'static str, color: Color| {
            let name = sys::power_name(key);
            button(text(name).size(13)).padding([7, 16]).style(ui::pill(color, cur == name)).on_press(Message::Power(key))
        };
        row![b("power-saver", ui::DSU), b("balanced", ui::GOOD), b("performance", ui::HOT)].spacing(8).into()
    }

    /// the jack's words and, when the board said which chip it is built with, the switch (Controls and the guide)
    pub(crate) fn jack_view(&self) -> Element<'_, Message> {
        let color = match self.jack {
            sys::Jack::Ok(_) => ui::GOOD,
            sys::Jack::Switch(_) | sys::Jack::Reboot(_) | sys::Jack::Keep(_) => ui::WARN,
            _ => ui::DIM,
        };
        let mut col = column![text(self.jack.words()).size(13).color(color)].spacing(8);
        if let Some(c) = self.jack.fix() {
            col = col.push(
                button(text(self.jack.fix_label()).size(13).font(ui::SEMIBOLD)).padding([7, 16]).style(ui::pill(ui::WARN, true))
                    .on_press_maybe((!self.busy).then_some(Message::CodecSet(c))),
            );
        }
        if !self.jack_error.is_empty() {
            col = col.push(text(self.jack_error.as_str()).size(12).color(ui::HOT));
        }
        col.into()
    }

    fn log_box(&self) -> Element<'_, Message> {
        let mut c = Column::new().spacing(2);
        for l in self.log.iter().rev().take(12).collect::<Vec<_>>().into_iter().rev() {
            c = c.push(text(l.clone()).size(12).font(iced::Font::MONOSPACE).color(if l.starts_with('$') { ui::BIG } else { ui::DIM }));
        }
        boxed(column![label("Output", ui::DIM), c].spacing(8))
    }

    fn hardware(&self) -> Element<'_, Message> {
        let p = &self.privileged;
        let on_gpu = self.procs.iter().filter(|x| x.gpu).count();
        let on_npu: Vec<&str> = self.procs.iter().filter(|x| x.npu).map(|x| x.name.as_str()).collect();
        let g = &self.gpu;
        let mut summary = row![chip(format!("GPU {} MHz", p.gpu_mhz.map(|v| v.to_string()).unwrap_or("—".into())), ui::GPU)].spacing(8);
        if let Some(l) = g.load {
            let part = |v: Option<u32>| v.map(|v| v.to_string()).unwrap_or("—".into());
            summary = summary.push(chip(
                format!("GPU busy {l}% (3D {} · geometry {} · compute {})", part(g.load_3d), part(g.load_geom), part(g.load_compute)),
                ui::GPU,
            ));
        }
        if let Some(m) = g.mem_mb {
            summary = summary.push(chip(format!("GPU memory {m} MB"), ui::GPU));
        }
        let summary = summary.extend([
            chip(
                match g.hwr {
                    Some(h) => format!("{h} GPU recoveries this boot"),
                    None => "GPU recoveries: —".into(),
                },
                if g.hwr.unwrap_or(0) == 0 { ui::GOOD } else { ui::HOT },
            ),
            chip(format!("{on_gpu} programs on the GPU"), ui::GPU),
            chip(if on_npu.is_empty() { "NPU idle".to_string() } else { format!("NPU: {}", on_npu.join(", ")) }, ui::NPU),
        ])
        .wrap();
        let head = row![
            text("Program").size(12).color(ui::DIM).width(Fill),
            text("PID").size(12).color(ui::DIM).width(64),
            text("CPU").size(12).color(ui::DIM).width(70),
            text("Memory").size(12).color(ui::DIM).width(84),
            text("Uses").size(12).color(ui::DIM).width(170),
        ]
        .spacing(10);
        let mut table = Column::new().spacing(4).push(head);
        for x in self.procs.iter().take(40) {
            let mut uses = row![].spacing(6);
            if x.gpu {
                uses = uses.push(badge("GPU", ui::GPU));
            }
            if x.display {
                uses = uses.push(badge("display", ui::BIG));
            }
            if x.npu {
                uses = uses.push(badge("NPU", ui::NPU));
            }
            if !x.mine {
                uses = uses.push(text("(other user)").size(11).color(ui::DIM));
            }
            table = table.push(
                container(
                    row![
                        text(x.name.clone()).size(13).width(Fill),
                        text(x.pid.to_string()).size(13).color(ui::DIM).width(64),
                        text(format!("{:.0}%", x.cpu * 100.0)).size(13).width(70).color(if x.cpu > 0.5 { ui::WARN } else { ui::TEXT }),
                        text(format!("{} MB", x.rss_mb)).size(13).width(84),
                        uses.width(170),
                    ]
                    .spacing(10)
                    .align_y(Alignment::Center),
                )
                .padding([6, 10])
                .style(|_: &iced::Theme| container::Style {
                    background: Some(iced::Background::Color(ui::CARD)),
                    border: iced::Border { color: ui::LINE, width: 1.0, radius: 8.0.into() },
                    ..Default::default()
                }),
            );
        }
        column![
            summary,
            text("Programs by CPU use; GPU, display and NPU are the devices a program has open. Other users' programs (the login screen, system services) can only show what they use with root's rights.")
                .size(12)
                .color(ui::DIM),
            table,
        ]
        .spacing(12)
        .into()
    }
}

// ---------------------------------------------------------------- the Assistant

impl Panel {
    fn assistant(&self) -> Element<'_, Message> {
        let (status, color) = match self.llm {
            "ready" => ("model ready", ui::GOOD),
            "loading" => ("loading the model…", ui::WARN),
            "starting" => ("starting the server…", ui::WARN),
            "stopped" => ("server stopped", ui::DIM),
            _ => ("checking the server…", ui::DIM),
        };
        let running = matches!(self.llm, "ready" | "loading");
        let server = button(text(if running { "Stop server" } else { "Start" }).size(13))
            .padding([7, 14])
            .style(ui::pill(if running { ui::HOT } else { ui::GOOD }, false))
            .on_press_maybe((!self.llm_busy).then_some(if running { Message::LlmStop } else { Message::LlmStart }));
        // who answers: this board's model, or one of the online assistants switched on (prefs::Online)
        let mut choices = vec![BOARD_MODEL.to_string()];
        choices.extend(self.prefs.online.iter().filter(|o| o.on).map(|o| o.name.clone()));
        let chosen = if self.prefs.ask_with.is_empty() { BOARD_MODEL.to_string() } else { self.prefs.ask_with.clone() };
        let online = self.prefs.online_named(&self.prefs.ask_with).is_some();
        let mut bar = row![].spacing(10).align_y(Alignment::Center);
        if choices.len() > 1 {
            bar = bar.push(pick_list(choices, Some(chosen), Message::AskWith).text_size(13));
        }
        // this board's model's own controls, while it is the one that answers
        if !online {
            bar = bar
                .push(chip(status.into(), color))
                .push(pick_list(self.models.clone(), self.model.clone(), Message::Model).text_size(13).placeholder("no models yet"))
                .push(server);
        } else if running {
            // it still holds its memory (about 6 GB) while another answers
            bar = bar.push(button(text("Stop the board\u{2019}s model").size(13)).padding([7, 14]).style(ui::pill(ui::HOT, false))
                .on_press_maybe((!self.llm_busy).then_some(Message::LlmStop)));
        }
        bar = bar.push(space::horizontal());
        bar = bar.push(button(text("Settings").size(13)).padding([7, 14]).style(ui::pill(ui::BIG, self.settings_open))
            .on_press_maybe((!self.settings_open).then_some(Message::SettingsOpen)));
        bar = bar.push(button(text("New chat").size(13)).padding([7, 14]).style(ui::pill(ui::BIG, false)).on_press(Message::NewChat));

        // how answers come: the web tools, and read aloud (a7a-voice: off while it cannot be asked)
        let speech = &self.speech;
        let mut how = row![
            toggler(self.web).label("web tools").on_toggle(Message::Web).text_size(13).size(18),
            toggler(self.speech_want.unwrap_or(speech.speak))
                .label("speak answers")
                .on_toggle_maybe((!self.speech_busy && !speech.busy && speech.error.is_empty()).then_some(Message::Speak))
                .text_size(13)
                .size(18),
        ]
        .spacing(18)
        .align_y(Alignment::Center);
        if let Some(p) = speech.setup.as_ref().filter(|p| p.error.is_empty()) {
            how = how.push(chip(format!("getting the voice… {:.0} %", p.fraction * 100.0), ui::WARN));
        } else if speech.busy {
            // the Voice tab's setup: it may be making a new venv, and checks the voice in it last
            how = how.push(chip("the voice commands are being set up…".into(), ui::DIM));
        } else if speech.speak && speech.ready && !speech.voices.is_empty() {
            let voices: Vec<voice::Voice> = speech.voices.iter().cloned().map(voice::Voice).collect();
            how = how.push(pick_list(voices, Some(voice::Voice(speech.voice.clone())), Message::SpeakVoice).text_size(13));
        } else if speech.speak && !speech.ready && !self.speech_busy && speech.error.is_empty() {
            // on, but the voice is gone (a setup of the listener's that failed, a new venv): said, not hidden
            how = how.push(chip("the voice is not set up: switch speak answers off and on".into(), ui::WARN));
        }
        how = how.push(space::horizontal());
        if let Some((r, n)) = self.rate {
            how = how.push(chip(format!("{r:.1} tokens/s · {n} tokens"), ui::NPU));
        }
        if self.reading > 0 || speech.speaking {
            how = how.push(button(text("Stop speaking").size(13)).padding([7, 14]).style(ui::pill(ui::HOT, false)).on_press(Message::Hush));
        }

        let mut log = Column::new().spacing(12).padding(iced::Padding { top: 4.0, right: 16.0, bottom: 4.0, left: 2.0 });
        if self.lines.iter().all(|l| matches!(l, Line::Note(..))) {
            log = log.push(self.welcome());
        }
        let last = self.lines.len().saturating_sub(1);
        for (i, l) in self.lines.iter().enumerate() {
            let item: Element<'_, Message> = match l {
                Line::User(q) => row![space::horizontal(), container(text(q).size(14)).padding([10, 14]).max_width(640).style(ui::bubble)].into(),
                Line::Answer(content, raw) => {
                    let body: Element<'_, Message> = if raw.is_empty() {
                        let waited = self.asked_at.elapsed().as_secs();
                        text(if i == last && self.answering { format!("reading the question… {waited} s") } else { String::new() })
                            .size(13)
                            .color(ui::DIM)
                            .into()
                    } else {
                        markdown::view(content.items(), md_settings()).map(Message::Link)
                    };
                    row![container(column![label(self.answerer(), ui::NPU), body].spacing(8)).padding([12, 16]).max_width(820).style(ui::card), space::horizontal()]
                        .into()
                }
                Line::Tool { audit, observation } => {
                    let preview = observation.lines().take(4).collect::<Vec<_>>().join("\n");
                    let blocked = audit.starts_with("BLOCKED");
                    container(
                        column![
                            row![badge("TOOL", if blocked { ui::WARN } else { ui::DSU }), text(audit).size(12).font(iced::Font::MONOSPACE)]
                                .spacing(8)
                                .align_y(Alignment::Center),
                            text(preview).size(11).font(iced::Font::MONOSPACE).color(ui::DIM),
                        ]
                        .spacing(6),
                    )
                    .padding([8, 12])
                    .max_width(820)
                    .style(ui::tool_box)
                    .into()
                }
                Line::Note(s, c) => text(s).size(12).color(*c).into(),
            };
            log = log.push(item);
        }
        let transcript = scrollable(log).anchor_bottom().height(Fill);

        let busy = self.answering || self.waiting.is_some();
        let action = if busy {
            button(text("Stop").size(14).font(ui::SEMIBOLD)).padding([11, 20]).style(ui::pill(ui::HOT, true)).on_press(Message::StopAnswer)
        } else {
            button(text("Send").size(14).font(ui::SEMIBOLD)).padding([11, 20]).style(ui::pill(ui::BIG, true)).on_press(Message::Send)
        };
        let ask = row![
            text_input(&match self.prefs.online_named(&self.prefs.ask_with) {
                Some(o) => format!("Ask {}…", o.name),
                None => "Ask about this board…".into(),
            }, &self.input).on_input(Message::Input).on_submit(Message::Send).padding(12).size(14).style(ui::input),
            action,
        ]
        .spacing(10)
        .align_y(Alignment::Center);

        let top = container(column![bar, how].spacing(10)).padding([10, 14]).style(ui::card);
        if self.settings_open {
            return column![top, self.assistant_settings()].spacing(12).height(Fill).into();
        }
        column![top, transcript, ask].spacing(12).height(Fill).into()
    }

    /// A row's test result and its test, or (None) every row's, gone: what they said was of the row as it was.
    fn online_untested(&mut self, row: Option<usize>) {
        match row {
            Some(i) => {
                self.online_tested.remove(&i);
                self.online_testing.remove(&i);
            }
            None => {
                self.online_tested.clear();
                self.online_testing.clear();
            }
        }
    }

    /// Who answers in the tab now, as its answers are labelled.
    fn answerer(&self) -> &str {
        match self.prefs.online_named(&self.prefs.ask_with) {
            Some(o) => o.name.as_str(),
            None => "Assistant",
        }
    }

    /// The settings page, in place of the chat: what the owner may change of the Assistant (prefs.rs).
    fn assistant_settings(&self) -> Element<'_, Message> {
        let e = &self.prefs_edit;
        let instructions = section(
            "Instructions",
            "Added to what the model is told before every question, here and by voice. For example: answer in two sentences; use metric units.",
            text_editor(&self.instructions).on_action(Message::Instructions).height(110).size(13).padding(10).into(),
        );

        let mut folders = Column::new().spacing(6);
        for (i, f) in e.folders.iter().enumerate() {
            folders = folders.push(row![text(f).size(13).font(iced::Font::MONOSPACE).width(Fill), small_button("Remove", Message::FolderRemove(i))]
                .spacing(10)
                .align_y(Alignment::Center));
        }
        folders = folders.push(
            row![
                text_input("/home/radxa/Documents or ~/Documents", &self.folder_input)
                    .on_input(Message::FolderInput)
                    .on_submit(Message::FolderAdd)
                    .padding(8)
                    .size(13)
                    .style(ui::input),
                small_button("Add", Message::FolderAdd),
            ]
            .spacing(10)
            .align_y(Alignment::Center),
        );
        let folders = section(
            "Folders it may read",
            "Its file tools (list, read, search) read only these folders and what is in them. None: everywhere except passwords, keys and browser data.",
            folders.into(),
        );

        let web = section(
            "Web tools",
            "Each one, while web tools is on (the switch above). Web search needs a SearXNG on this board (127.0.0.1:8888).",
            row![
                toggler(e.web_search).label("web search").on_toggle(Message::WebSearch).text_size(13).size(18),
                toggler(e.fetch_url).label("read web pages").on_toggle(Message::FetchUrl).text_size(13).size(18),
                toggler(e.weather).label("weather").on_toggle(Message::Weather).text_size(13).size(18),
            ]
            .spacing(24)
            .into(),
        );

        let mut servers = Column::new().spacing(12);
        for (i, sv) in e.mcp.iter().enumerate() {
            let key = prefs::as_saved(sv).map(|s| mcp_key(&s)).unwrap_or_default();
            let found: Element<'_, Message> = match (self.mcp_loading.contains(&key), self.mcp_found.get(&key)) {
                (true, _) => text("starting it and asking for its tools… (a first start may download it)").size(12).color(ui::DIM).into(),
                (false, Some(Ok(tools))) if tools.is_empty() => text("it has no tools").size(12).color(ui::DIM).into(),
                (false, Some(Ok(tools))) => {
                    let mut r = row![].spacing(14);
                    for t in tools {
                        let name = t.name.clone();
                        r = r.push(toggler(!sv.off_tools.contains(&t.name)).label(t.name.clone()).on_toggle(move |b| Message::McpTool(i, name.clone(), b)).text_size(12).size(16));
                    }
                    r.wrap().into()
                }
                (false, Some(Err(err))) => text(format!("did not start: {err}")).size(12).color(ui::HOT).into(),
                (false, None) => space::vertical().height(0).into(),
            };
            servers = servers.push(
                container(
                    column![
                        row![
                            text_input("name", &sv.name).on_input(move |v| Message::McpName(i, v)).padding(8).size(13).width(130).style(ui::input),
                            text_input("command, e.g. npx -y @modelcontextprotocol/server-filesystem /home/radxa/Documents", &sv.command)
                                .on_input(move |v| Message::McpCommand(i, v))
                                .padding(8)
                                .size(13)
                                .style(ui::input),
                            toggler(sv.on).label("on").on_toggle(move |b| Message::McpOn(i, b)).text_size(13).size(18),
                            small_button("Remove", Message::McpRemove(i)),
                        ]
                        .spacing(10)
                        .align_y(Alignment::Center),
                        row![
                            text_input("environment: KEY=value … (a token, say)", &sv.env).on_input(move |v| Message::McpEnv(i, v)).padding(8).size(13).style(ui::input),
                            pick_list(prefs::Reach::ALL, Some(sv.reach), move |r| Message::McpReach(i, r)).text_size(13),
                            small_button("Its tools", Message::McpLoad(i)),
                        ]
                        .spacing(10)
                        .align_y(Alignment::Center),
                        found,
                    ]
                    .spacing(8),
                )
                .padding(10)
                .style(ui::tool_box),
            );
        }
        servers = servers.push(small_button("Add a server", Message::McpAdd));
        let mcp = section(
            "MCP servers",
            "Programs that give it more tools (the Model Context Protocol, over stdin and stdout). Each runs as you, with your rights: add only ones you trust, and switch off any of their tools that write, move or run things (Its tools): such a tool could change these settings, and so start programs. One that reaches the internet pauses in a chat that has read this board, as the web tools do; one that does both gets a chat to itself (and can still pass on what it reads through its own tools). Every tool makes questions slower: the model reads them all first.",
            servers.into(),
        );

        let mut assistants = Column::new().spacing(12);
        for (i, o) in e.online.iter().enumerate() {
            let tested: Element<'_, Message> = match (self.online_testing.contains_key(&i), self.online_tested.get(&i)) {
                (true, _) => text("asking it…").size(12).color(ui::DIM).into(),
                (false, Some(Ok(answer))) => text(format!("it answered: {}", answer.trim())).size(12).color(ui::GOOD).into(),
                (false, Some(Err(err))) => text(err.as_str()).size(12).color(ui::HOT).into(),
                (false, None) => text(format!("key from {}", o.provider.keys_at())).size(12).color(ui::DIM).into(),
            };
            let mut second = row![
                text_input(if o.provider.model().is_empty() { "model (its name at the provider)" } else { o.provider.model() }, &o.model)
                    .on_input(move |v| Message::OnlineModel(i, v))
                    .padding(8)
                    .size(13)
                    .width(220)
                    .style(ui::input),
                text_input("API key", &o.key).on_input(move |v| Message::OnlineKey(i, v)).secure(true).padding(8).size(13).style(ui::input),
            ]
            .spacing(10)
            .align_y(Alignment::Center);
            match o.provider {
                prefs::Provider::Other => {
                    second = second.push(text_input("https://…/v1 (its chat completions under it)", &o.url).on_input(move |v| Message::OnlineUrl(i, v)).padding(8).size(13).style(ui::input));
                }
                // Workers AI's address is made of the Account ID (prefs::Online::url); an AI Gateway's whole https://
                // address goes here too
                prefs::Provider::Cloudflare => {
                    second = second.push(text_input("Account ID (32 letters and digits)", &o.url).on_input(move |v| Message::OnlineUrl(i, v)).padding(8).size(13).style(ui::input));
                }
                _ => {}
            }
            second = second.push(small_button("Test", Message::OnlineTest(i)));
            assistants = assistants.push(
                container(
                    column![
                        row![
                            text_input("name (hey …)", &o.name).on_input(move |v| Message::OnlineName(i, v)).padding(8).size(13).width(150).style(ui::input),
                            pick_list(prefs::Provider::ALL, Some(o.provider), move |pr| Message::OnlineProvider(i, pr)).text_size(13),
                            space::horizontal(),
                            toggler(o.on).label("on").on_toggle(move |b| Message::OnlineOn(i, b)).text_size(13).size(18),
                            small_button("Remove", Message::OnlineRemove(i)),
                        ]
                        .spacing(10)
                        .align_y(Alignment::Center),
                        second,
                        tested,
                    ]
                    .spacing(8),
                )
                .padding(10)
                .style(ui::tool_box),
            );
        }
        assistants = assistants.push(small_button("Add an online assistant", Message::OnlineAdd));
        let online_section = section(
            "Online assistants",
            "Other assistants, on the internet: choose one under \"ask\" above, or say \"hey\" and its name (one of three letters or more, that does not begin like another wake phrase: the Voice tab says which it does not hear). What you ask one goes to its provider, with your key; it gets none of this board's tools, nor your instructions. DuckDuckGo is not here: its AI chat may not be used by programs (its terms).",
            assistants.into(),
        );

        let memory = section(
            "Memory and length",
            "More memory or context makes every answer slower here: the model rereads it all first. A new context is used from the model server's next start; 16384 needs about 1.2 GB more memory.",
            row![
                text("remembers the last").size(13),
                pick_list(prefs::MEMORIES, Some(e.memory), Message::Memory).text_size(13),
                text("exchanges").size(13),
                space::horizontal().width(20),
                text("answers up to").size(13),
                pick_list(prefs::ANSWER_LENGTHS, Some(e.answer_length), Message::AnswerLength).text_size(13),
                text("tokens").size(13),
                space::horizontal().width(20),
                text("context").size(13),
                pick_list(prefs::CONTEXTS, Some(e.context), Message::Context).text_size(13),
                text("tokens").size(13),
            ]
            .spacing(8)
            .align_y(Alignment::Center)
            .wrap()
            .into(),
        );

        let mut actions = row![
            button(text("Save").size(14).font(ui::SEMIBOLD)).padding([10, 22]).style(ui::pill(ui::BIG, true)).on_press(Message::SettingsSave),
            button(text("Cancel").size(14)).padding([10, 22]).style(ui::pill(ui::DIM, false)).on_press(Message::SettingsCancel),
        ]
        .spacing(10)
        .align_y(Alignment::Center);
        if !self.settings_error.is_empty() {
            actions = actions.push(text(&self.settings_error).size(13).color(ui::HOT));
        }
        let mut page = column![instructions, folders, web, mcp, online_section, memory, actions].spacing(12);
        if self.prefs_error.is_empty() && !e.folders_error.is_empty() {
            page = column![text(format!("The file tools are off: {}. Add the folders it may read below, or Save with none to let it read everywhere except passwords, keys and browser data.", e.folders_error)).size(13).color(ui::WARN), page]
                .spacing(12);
        }
        if !self.prefs_error.is_empty() {
            page = column![text(format!("The settings file does not read ({}): these are the defaults; Save writes it again.", self.prefs_error)).size(13).color(ui::WARN), page]
                .spacing(12);
        }
        scrollable(container(page).padding(iced::Padding { top: 0.0, right: 14.0, bottom: 8.0, left: 0.0 })).height(Fill).into()
    }

    fn welcome(&self) -> Element<'_, Message> {
        if let Some(o) = self.prefs.online_named(&self.prefs.ask_with) {
            return boxed(
                column![
                    label(&format!("Ask {}", o.name), ui::NPU),
                    text(format!("{} answers here, over the internet: what you ask goes to {} with your key, and its answer comes back from there. It knows nothing of this board and has none of its tools, nor your instructions: for those, choose this board\u{2019}s model under ask.", o.name, o.provider))
                        .size(13),
                    text(format!("{}With the voice commands on, \u{201c}hey {}\u{201d} asks it by voice. Speak answers reads its answers aloud, in the voice made on this board. New chat starts afresh.",
                        if o.provider == prefs::Provider::Brave { "Brave answers each question on its own, from its search: it does not see the ones before. " } else { "" }, o.name))
                        .size(12)
                        .color(ui::DIM),
                ]
                .spacing(12),
            );
        }
        let mut tries = row![].spacing(8);
        for q in SUGGESTIONS {
            tries = tries.push(button(text(q).size(13)).padding([7, 14]).style(ui::pill(ui::NPU, false)).on_press(Message::Ask(q.to_string())));
        }
        let tries = tries.wrap();
        let get: Element<'_, Message> = match (self.download, self.models.is_empty()) {
            (Some(f), _) => column![
                text(format!("Downloading the model… {:.0}% of 2.4 GB", f * 100.0)).size(13).color(ui::NPU),
                canvas(ui::Bar { value: f, color: ui::NPU }).width(Fill).height(10),
            ]
            .spacing(8)
            .into(),
            (None, true) => row![
                text("There is no model on this board yet. The default one, Qwen3-4B-Instruct (2.4 GB), takes a few minutes to download.")
                    .size(13)
                    .width(Fill),
                button(text("Download it").size(13).font(ui::SEMIBOLD)).padding([8, 16]).style(ui::pill(ui::NPU, true)).on_press(Message::LlmGet),
            ]
            .spacing(12)
            .align_y(Alignment::Center)
            .into(),
            (None, false) => tries.into(),
        };
        boxed(
            column![
                label("Ask this board", ui::NPU),
                text("A language model running here, on the two big cores: about three words a second, and nothing you ask leaves the board. It can look at this machine (temperatures, memory, processes, files) but cannot change anything.")
                    .size(13),
                text("A chat uses this board's tools or the web, not both: once it has looked at the board its web tools pause, once it has read the web the board's tools pause, so nothing from here can be sent out through a link. New chat starts afresh.")
                    .size(12)
                    .color(ui::DIM),
                text("Speak answers reads each answer aloud, here and to \u{201c}hey assistant\u{201d}, in a voice made on this board (about 130 MB, downloaded the first time). Stop speaking, or a wake phrase said to the microphone, stops it.")
                    .size(12)
                    .color(ui::DIM),
                get,
            ]
            .spacing(12),
        )
    }
}

// ---------------------------------------------------------------- the Voice tab

/// The Test and remove buttons' widths, which the commands' header leaves free so its words sit over their boxes.
const TEST_W: u32 = 64;
const REMOVE_W: u32 = 36;

impl Panel {
    fn voice_tab(&self) -> Element<'_, Message> {
        let v = &self.voice;
        let busy = self.voice_busy;
        let setting_up = v.setup.as_ref().filter(|p| p.error.is_empty());
        let (state, color) = if !self.voice_known {
            ("asking the listener…".to_string(), ui::DIM)
        } else if let Some(p) = setting_up {
            (format!("setting up… {:.0}%", p.fraction * 100.0), ui::WARN)
        } else if v.running {
            match v.state.as_str() {
                "awake" => ("heard the wake phrase…".to_string(), ui::NPU),
                "dictating" => ("dictating: say \u{201c}stop dictation\u{201d} when done".to_string(), ui::NPU),
                "starting" => ("starting… (loading the models)".to_string(), ui::WARN),
                "error" => ("error".to_string(), ui::HOT),
                _ => ("listening".to_string(), ui::GOOD),
            }
        } else if v.error == voice::NOT_INSTALLED {
            ("not installed".to_string(), ui::HOT)
        } else if !v.ready {
            ("not set up".to_string(), ui::DIM)
        } else if !v.error.is_empty() {
            ("stopped on an error".to_string(), ui::HOT)
        } else {
            ("off".to_string(), ui::DIM)
        };
        let toggle = if v.running {
            button(text("Turn off").size(13)).padding([7, 16]).style(ui::pill(ui::HOT, false)).on_press_maybe((!busy).then_some(Message::VoiceOn(false)))
        } else {
            button(text("Turn on").size(13).font(ui::SEMIBOLD))
                .padding([7, 16])
                .style(ui::pill(ui::GOOD, v.ready))
                .on_press_maybe((!busy && v.ready).then_some(Message::VoiceOn(true)))
        };
        let mut bar = row![chip(state, color), toggle].spacing(10).align_y(Alignment::Center);
        let can_set_up = self.voice_known && !v.ready && setting_up.is_none() && v.error != voice::NOT_INSTALLED;
        if can_set_up {
            bar = bar.push(button(text("Set up").size(13).font(ui::SEMIBOLD)).padding([7, 16]).style(ui::pill(ui::NPU, true)).on_press_maybe((!busy).then_some(Message::VoiceSetup)));
        }
        if v.running && v.enabled {
            bar = bar.push(text("starts at every log-in").size(12).color(ui::DIM));
        }
        let mut status = column![label("Voice commands", ui::NPU), bar].spacing(10);
        if let Some(p) = setting_up {
            status = status.push(
                column![
                    text(if p.step.is_empty() { "Setting up…".to_string() } else { format!("Setting up: {}", p.step) }).size(13).color(ui::NPU),
                    canvas(ui::Bar { value: p.fraction, color: ui::NPU }).width(Fill).height(10),
                ]
                .spacing(8),
            );
        } else if can_set_up {
            status = status.push(
                text("Set up downloads the speech engine and two English models, about 110 MB, into ~/.local/share/a7a-voice: a minute or two.")
                    .size(12)
                    .color(ui::DIM),
            );
        }
        if let Some(e) = v.setup.as_ref().map(|p| p.error.as_str()).filter(|e| !e.is_empty()) {
            status = status.push(text(format!("The setup stopped: {e}")).size(12).color(ui::HOT));
        }
        if !v.heard.is_empty() {
            let last = if v.ran.is_empty() {
                format!("Heard \u{201c}{}\u{201d}: no command matched", v.heard)
            } else {
                format!("Heard \u{201c}{}\u{201d} \u{2192} ran \u{201c}{}\u{201d}", v.heard, v.ran)
            };
            status = status.push(text(last).size(13).color(if v.ran.is_empty() { ui::WARN } else { ui::TEXT }));
        }
        // the listener's own "not set up" (it was started before the setup) says what the chip and Set up say already
        if !v.error.is_empty() && v.error != voice::NOT_INSTALLED && !(can_set_up && v.error.starts_with("not set up")) {
            status = status.push(text(v.error.clone()).size(12).color(ui::HOT));
        }
        if v.error == voice::NOT_INSTALLED {
            status = status.push(text(voice::NOT_INSTALLED).size(12).color(ui::HOT));
        }
        if v.running && !v.left_out.is_empty() {
            // an online assistant's name the spotter cannot tell apart (the Assistant's settings name them)
            status = status.push(text(format!("Not listening for {}.", v.left_out.join("; "))).size(12).color(ui::WARN));
        }
        if !v.config_error.is_empty() {
            status = status.push(
                text(format!("The settings file does not read ({}). Saving the settings below writes it again.", v.config_error))
                    .size(12)
                    .color(ui::WARN),
            );
        }
        status = status.push(
            text("Say a wake phrase, then a command or a question, in one breath or after a pause. It listens on this board, about 15 % of one small core while it waits, and nothing is written down before a wake phrase. What you say stays on the board but for a question to an online assistant (it goes to that one's provider), the words \u{201c}go to \u{2026}\u{201d} searches for, and what the Assistant looks up with its web tools, as in its tab.")
                .size(12)
                .color(ui::DIM),
        );

        // (every box is locked while a save, a test or a scan runs: an edit then would keep the editor from taking the
        // settings they leave, and the next Save would write the old ones back: review 2026-11-22)
        let wake_box = text_input("hey cubie", &self.voice_edit.wake).on_input_maybe((!busy).then_some(Message::VoiceWake)).padding(10).size(14).style(ui::input);
        let assistant_box = text_input("hey assistant (empty: none)", &self.voice_edit.assistant)
            .on_input_maybe((!busy).then_some(Message::VoiceAssistant))
            .padding(10)
            .size(14)
            .style(ui::input);
        let phrase_row = |name: &'static str, input: iced::widget::TextInput<'static, Message>| -> Element<'static, Message> {
            row![text(name).size(13).color(ui::DIM).width(150), input].spacing(10).align_y(Alignment::Center).into()
        };

        let (say_w, run_w) = (Length::FillPortion(7), Length::FillPortion(13));
        let mut list = Column::new().spacing(8).push(
            row![
                text("You say").size(12).color(ui::DIM).width(say_w),
                text("It runs").size(12).color(ui::DIM).width(run_w),
                space::Space::new().width(TEST_W + REMOVE_W + 8),
            ]
            .spacing(8),
        );
        for (i, c) in self.voice_edit.commands.iter().enumerate() {
            let complete = !c.say.trim().is_empty() && !c.run.trim().is_empty();
            list = list.push(
                row![
                    text_input("what you say", &c.say).on_input_maybe((!busy).then_some(move |s| Message::VoiceSay(i, s))).padding(8).size(13).style(ui::input).width(say_w),
                    text_input("the command line", &c.run)
                        .on_input_maybe((!busy).then_some(move |s| Message::VoiceRun(i, s)))
                        .padding(8)
                        .size(13)
                        .font(iced::Font::MONOSPACE)
                        .style(ui::input)
                        .width(run_w),
                    button(text("Test").size(12).width(Fill).align_x(Alignment::Center))
                        .width(TEST_W)
                        .padding([6, 8])
                        .style(ui::pill(ui::DSU, false))
                        .on_press_maybe((!busy && complete).then_some(Message::VoiceTest(i))),
                    button(text("\u{d7}").size(15).width(Fill).align_x(Alignment::Center))
                        .width(REMOVE_W)
                        .padding([4, 6])
                        .style(ui::pill(ui::HOT, false))
                        .on_press_maybe((!busy).then_some(Message::VoiceRemove(i))),
                ]
                .spacing(8)
                .align_y(Alignment::Center),
            );
        }
        if self.voice_edit.commands.is_empty() {
            list = list.push(text("No commands yet: add one.").size(13).color(ui::DIM));
        }
        let mut actions = row![
            button(text("+ Add a command").size(13)).padding([7, 14]).style(ui::pill(ui::BIG, false)).on_press_maybe((!busy).then_some(Message::VoiceAdd)),
            space::horizontal(),
        ]
        .spacing(10)
        .align_y(Alignment::Center);
        if self.voice_dirty {
            actions = actions.push(text("unsaved changes").size(12).color(ui::WARN));
        }

        // the found commands: keys and the desktop always shown, the applications' folded away at first
        let found = &self.voice_edit.found;
        let on = found.iter().filter(|f| f.on).count();
        let mut scan_bar = row![
            button(text(if found.is_empty() { "Scan this board" } else { "Scan again" }).size(13).font(ui::SEMIBOLD))
                .padding([7, 16])
                .style(ui::pill(ui::NPU, found.is_empty()))
                .on_press_maybe((!busy).then_some(Message::VoiceScan)),
        ]
        .spacing(10)
        .align_y(Alignment::Center);
        if !found.is_empty() {
            scan_bar = scan_bar.push(text(format!("{} found, {on} on", found.len())).size(12).color(ui::DIM));
        }
        if self.voice_dirty {
            // (the Commands' Save is far above once the list is open)
            scan_bar = scan_bar.push(space::horizontal()).push(text("unsaved changes").size(12).color(ui::WARN)).push(
                button(text("Save").size(13).font(ui::SEMIBOLD)).padding([7, 20]).style(ui::pill(ui::BIG, true)).on_press_maybe((!busy).then_some(Message::VoiceSave)),
            );
        }
        let mut found_list = Column::new().spacing(6).push(scan_bar);
        let twice = self.voice_edit.said_twice();
        if !twice.is_empty() {
            found_list = found_list.push(
                text(format!(
                    "Said for more than one command (only the first runs): {}. Change the words of the others, or switch them off.",
                    twice.iter().map(|w| format!("\u{201c}{w}\u{201d}")).collect::<Vec<_>>().join(", ")
                ))
                .size(12)
                .color(ui::WARN),
            );
        }
        let found_row = |i: usize, f: &voice::Found| -> Element<'_, Message> {
            row![
                // (locked while a scan or a save runs: an edit then would undo the scan at the next Save)
                toggler(f.on).on_toggle_maybe((!busy).then_some(move |b| Message::VoiceFoundOn(i, b))).size(18),
                text_input("what you say", &f.say)
                    .on_input_maybe((!busy).then_some(move |s| Message::VoiceFoundSay(i, s)))
                    .padding(6)
                    .size(13)
                    .style(ui::input)
                    .width(say_w),
                text(f.run.clone()).size(11).font(iced::Font::MONOSPACE).color(ui::DIM).width(run_w),
                button(text("Test").size(12).width(Fill).align_x(Alignment::Center))
                    .width(TEST_W)
                    .padding([5, 8])
                    .style(ui::pill(ui::DSU, false))
                    .on_press_maybe((!busy && f.on && !f.say.trim().is_empty()).then_some(Message::VoiceFoundTest(i))),
            ]
            .spacing(8)
            .align_y(Alignment::Center)
            .into()
        };
        let apps = found.iter().filter(|f| f.kind == "app").count();
        // under their groups, in voice::GROUPS' order, then any other kind under "Other"
        let known = |k: &str| voice::GROUPS.iter().any(|(g, _)| *g == k);
        let mut groups: Vec<(&str, Vec<usize>)> = voice::GROUPS
            .iter()
            .map(|(g, title)| (*title, found.iter().enumerate().filter(|(_, f)| f.kind == *g).map(|(i, _)| i).collect()))
            .collect();
        groups.push(("Other", found.iter().enumerate().filter(|(_, f)| f.kind != "app" && !known(&f.kind)).map(|(i, _)| i).collect()));
        for (title, members) in groups.into_iter().filter(|(_, m)| !m.is_empty()) {
            found_list = found_list.push(
                container(text(format!("{title} ({})", members.len())).size(12).font(ui::SEMIBOLD).color(ui::DIM))
                    .padding(iced::Padding { top: 6.0, ..Default::default() }),
            );
            for i in members {
                found_list = found_list.push(found_row(i, &found[i]));
            }
        }
        if apps > 0 {
            found_list = found_list.push(
                row![
                    text(format!("Applications ({apps} commands)")).size(12).font(ui::SEMIBOLD).color(ui::DIM),
                    button(text(if self.voice_show_apps { "Hide" } else { "Show" }).size(12))
                        .padding([4, 12])
                        .style(ui::pill(ui::BIG, false))
                        .on_press(Message::VoiceShowApps(!self.voice_show_apps)),
                ]
                .spacing(10)
                .align_y(Alignment::Center),
            );
            if self.voice_show_apps {
                for (i, f) in found.iter().enumerate().filter(|(_, f)| f.kind == "app") {
                    found_list = found_list.push(found_row(i, f));
                }
            }
        }
        actions = actions.push(
            button(text("Save").size(13).font(ui::SEMIBOLD))
                .padding([7, 20])
                .style(ui::pill(ui::BIG, self.voice_dirty))
                .on_press_maybe((self.voice_dirty && !busy).then_some(Message::VoiceSave)),
        );

        // who answers which wake phrase, as saved: the commands, this board's Assistant, the online ones switched on
        let who_row = |phrase: String, color: Color, title: &'static str, detail: String, warn: bool| -> Element<'static, Message> {
            row![
                container(chip(phrase, color)).width(170),
                column![text(title).size(13).font(ui::SEMIBOLD), text(detail).size(12).color(if warn { ui::WARN } else { ui::DIM })].spacing(2).width(Fill),
            ]
            .spacing(12)
            .align_y(Alignment::Center)
            .into()
        };
        let quoted = |p: &str| format!("\u{201c}{p}\u{201d}");
        let mut who = Column::new().spacing(10);
        // (the listener wakes only for those with a key: a7a-voice online_names; its list is empty while it starts)
        let listening = v.running && !matches!(v.state.as_str(), "starting" | "error");
        // a phrase the listener did not take (it kept the old one): not shown as working (2026-11-24)
        let refused = |saved: &str, heard: Option<&String>| if listening { voice::refused(saved, heard.map(String::as_str)) } else { None };
        if !v.config.wake.is_empty() {
            let own = v.config.commands.iter().filter(|c| !c.say.trim().is_empty() && !c.run.trim().is_empty()).count();
            let found_on = v.config.found.iter().filter(|f| f.on).count();
            let (detail, warn) = match refused(&v.config.wake, v.listening.as_ref().map(|l| &l.0)) {
                Some(why) => (why, true),
                None => (format!("{} commands: {own} of yours and {found_on} found on this board, and typing by voice (all below)", own + found_on), false),
            };
            who = who.push(who_row(quoted(&v.config.wake), ui::NPU, "Commands", detail, warn));
        }
        if !v.config.assistant.is_empty() {
            // a question by voice starts the model server when it is not up (a7a-llm serve: its default model), else
            // the model it runs answers, whichever the Assistant tab started (review 2026-11-23)
            let model = agent::DEFAULT_MODEL.trim_end_matches(".gguf");
            let (detail, warn) = if let Some(why) = refused(&v.config.assistant, v.listening.as_ref().map(|l| &l.1)) {
                (why, true)
            } else if self.models.iter().any(|m| m == agent::DEFAULT_MODEL) {
                (format!("{model} (or the model the Assistant tab\u{2019}s server runs) \u{b7} answers in a notification, and aloud with speak answers on in that tab"), false)
            } else {
                (format!("{model} is not downloaded yet: the Assistant tab gets it (until then a question by voice fails)"), true)
            };
            who = who.push(who_row(quoted(&v.config.assistant), ui::BIG, "The Assistant, on this board", detail, warn));
        }
        for o in self.prefs.online.iter().filter(|o| o.on) {
            let (detail, warn) = if o.key.trim().is_empty() {
                ("no key yet: give it one in the Assistant tab\u{2019}s Settings (until then it is not asked by voice)".to_string(), true)
            } else if o.model().is_empty() {
                ("no model chosen: choose one in the Assistant tab\u{2019}s Settings".to_string(), true)
            } else if listening && !v.online.contains(&o.name) {
                (if v.left_out.is_empty() { "the listener does not wake for this name yet" } else { "the listener does not wake for this name (why: above)" }.to_string(), true)
            } else {
                (format!("{} at {} \u{b7} what you ask goes there", short_model(o.model()), provider_place(o)), false)
            };
            who = who.push(who_row(quoted(&format!("hey {}", o.name)), ui::RAM, "Online assistant", detail, warn));
        }
        if !self.voice_known {
            who = who.push(text("Asking the listener for its phrases\u{2026}").size(13).color(ui::DIM));
        }

        // typing by voice: the commands' phrase's own phrases (built in)
        let phrase_line = |say: &'static str, does: &'static str| -> Element<'static, Message> {
            row![container(chip(say.to_string(), ui::NPU)).width(200), text(does).size(12).color(ui::DIM).width(Fill)]
                .spacing(12)
                .align_y(Alignment::Center)
                .into()
        };
        let typing = column![
            phrase_line("type \u{2026}", "types the rest where the cursor is: \u{201c}type google dot com\u{201d}, \u{201c}type see you soon period\u{201d}"),
            phrase_line("go to \u{2026}", "opens a site in the browser (\u{201c}go to lemonde dot fr\u{201d}); anything else is searched for"),
            phrase_line("delete that", "takes back what was typed last"),
            phrase_line("start dictation", "types each sentence you say, without the wake phrase, until \u{201c}stop dictation\u{201d} (or a wake phrase, or a minute of silence): everything said meanwhile is typed"),
            phrase_line("comma \u{b7} period \u{b7} new line", "punctuation, said inside \u{201c}type \u{2026}\u{201d} or dictation where it goes; also \u{201c}question mark\u{201d}, \u{201c}exclamation mark\u{201d}, \u{201c}new paragraph\u{201d}"),
        ]
        .spacing(8);
        let after = if v.config.wake.is_empty() { "the commands\u{2019} wake phrase".to_string() } else { quoted(&v.config.wake) };

        column![
            boxed(status),
            section("Who answers", "Say one of these, then what you want: in one breath, or after a pause.".to_string(), who.into()),
            section(
                "Wake phrases",
                "Common words, spelled as they are said, work best: the wake-word model knows words by their letters (\u{201c}hey nova\u{201d} wakes it, \u{201c}hey qwen\u{201d} never did). The online assistants\u{2019} names are set in the Assistant tab\u{2019}s Settings: \u{201c}hey\u{201d} and the name.".to_string(),
                column![
                    phrase_row("For commands", wake_box),
                    phrase_row("For the Assistant", assistant_box),
                    text("After the Assistant\u{2019}s phrase, ask a question: the Assistant tab\u{2019}s model answers, with its look at the board, in a notification (and aloud, with speak answers on in the Assistant tab). The first question starts the model: a minute or two, then about 6 GB of memory until Stop server in the Assistant tab. Empty: no Assistant by voice.")
                        .size(12)
                        .color(ui::DIM),
                ]
                .spacing(10)
                .into()
            ),
            section(
                "Typing by voice",
                format!("After {after}, where the cursor is (an address bar, a chat box). Say an address\u{2019}s dots as \u{201c}dot\u{201d}; \u{201c}www\u{201d} may be left out (the browser adds it). While dictating, a typing key\u{2019}s words alone run it (\u{201c}press enter\u{201d}, \u{201c}select all\u{201d}); anything else said is typed."),
                typing.into()
            ),
            section(
                "Your commands",
                format!("After {after}. What runs is a command line, as you: firefox \u{b7} wpctl set-volume @DEFAULT_AUDIO_SINK@ 10%+ \u{b7} notify-send Hello. Numbers may be typed as digits. Test runs the command its words match now (saving first)."),
                column![list, actions].spacing(12).into()
            ),
            section(
                "Found on this board",
                format!("After {after}. Scan finds the menu\u{2019}s applications (\u{201c}open \u{2026}\u{201d}, \u{201c}close \u{2026}\u{201d}: closed as their \u{d7} would, so they may ask to save) and common commands. Change the words of one, or switch it off, then Save: a new scan keeps that, adds new applications and drops removed ones. Restart, shut down and Wi-Fi off start switched off: misheard, they would cut you off from the board."),
                found_list.into()
            ),
            text("Speech: sherpa-onnx (Apache-2.0) and its GigaSpeech wake-word model, with Kroko's English model (CC-BY-SA, kroko.ai).").size(11).color(ui::DIM),
            self.log_box(),
        ]
        .spacing(14)
        .into()
    }
}

fn md_settings() -> markdown::Settings {
    let style = markdown::Style {
        inline_code_highlight: markdown::Highlight { background: ui::CARD_HI.into(), border: iced::border::rounded(4) },
        inline_code_color: ui::RAM,
        link_color: ui::BIG,
        ..markdown::Style::from_palette(ui::theme().palette())
    };
    markdown::Settings::with_text_size(14, style)
}

// ---------------------------------------------------------------- small builders

fn power_color(p: &str) -> Color {
    match p {
        "Power Save" => ui::DSU,
        "Performance" => ui::HOT,
        "Balanced" => ui::GOOD,
        _ => ui::DIM,
    }
}

/// A server's place in the settings page's tool lists (of one as saved: prefs::as_saved): its name and command.
fn mcp_key(sv: &prefs::Server) -> String {
    format!("{}\n{}", sv.name, sv.command)
}

fn small_button(label: &str, on: Message) -> Element<'_, Message> {
    button(text(label).size(13)).padding([6, 12]).style(ui::pill(ui::BIG, false)).on_press(on).into()
}

/// A model's name without its provider's path ("@cf/openai/gpt-oss-120b" is gpt-oss-120b).
fn short_model(m: &str) -> &str {
    m.rsplit('/').next().unwrap_or(m)
}

/// Where an online assistant answers from: the host of an address of the owner's ("cloudflare.com"), else its provider.
fn provider_place(o: &prefs::Online) -> String {
    if o.provider == prefs::Provider::Other {
        let host = o.url.split("://").nth(1).unwrap_or(&o.url).split('/').next().unwrap_or("");
        host.strip_prefix("api.").unwrap_or(host).to_string()
    } else {
        o.provider.to_string().split(" (").next().unwrap_or("").to_string()
    }
}

fn chip<'a>(s: String, c: Color) -> Element<'a, Message> {
    container(text(s).size(12).font(ui::SEMIBOLD)).padding([4, 10]).style(ui::chip(c)).into()
}

fn badge<'a>(s: &'a str, c: Color) -> Element<'a, Message> {
    container(text(s).size(11).font(ui::SEMIBOLD)).padding([2, 8]).style(ui::chip(c)).into()
}

fn tab_button(name: &str, tab: Tab, current: Option<Tab>) -> Element<'_, Message> {
    button(text(name).size(14).font(ui::SEMIBOLD)).padding([7, 16]).style(ui::tab(Some(tab) == current)).on_press(Message::Tab(tab)).into()
}

fn label<'a>(s: &str, c: Color) -> Element<'a, Message> {
    row![container(space::horizontal()).width(4).height(14).style(move |_: &iced::Theme| container::Style {
        background: Some(iced::Background::Color(c)),
        border: iced::Border { radius: 2.0.into(), ..Default::default() },
        ..Default::default()
    }), text(s.to_uppercase()).size(12).font(ui::BOLD).color(ui::DIM)]
    .spacing(8)
    .align_y(Alignment::Center)
    .into()
}

fn boxed<'a>(content: impl Into<Element<'a, Message>>) -> Element<'a, Message> {
    container(content).padding(16).width(Fill).style(ui::card).into()
}

fn card<'a>(title: &'a str, accent: Color, value: String, sub: String, chart: Option<Element<'a, Message>>) -> Element<'a, Message> {
    let mut c = column![
        label(title, accent),
        text(value).size(30).font(ui::BOLD).color(accent),
        text(sub).size(12).color(ui::DIM),
    ]
    .spacing(6);
    if let Some(ch) = chart {
        c = c.push(ch);
    }
    boxed(c)
}

fn spark(values: &VecDeque<f32>, color: Color) -> Element<'_, Message> {
    canvas(ui::Sparkline { values, color, capacity: HISTORY }).width(Fill).height(Length::Fixed(52.0)).into()
}

fn section<'a>(title: &'a str, hint: impl iced::widget::text::IntoFragment<'a>, content: Element<'a, Message>) -> Element<'a, Message> {
    boxed(column![label(title, ui::BIG), text(hint).size(12).color(ui::DIM), content].spacing(10))
}

fn list(v: &[u32]) -> String {
    v.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(" · ")
}

/// The tools of a question asked from the command line: the owner's settings (as the tab's), MCP servers of its own.
fn cli_tools() -> (agent::Tools, Arc<mcp::Pool>) {
    let (p, why) = prefs::Prefs::load(&prefs::Prefs::path());
    if !why.is_empty() {
        eprintln!("the Assistant's settings do not read ({why}): the defaults instead");
    }
    let pool = Arc::new(mcp::Pool::default());
    (agent::Tools::new(Arc::new(agent::Taint::default()), agent::Settings::from_prefs(&p, p.web), pool.clone()), pool)
}

/// One question from the command line: to the online assistant `to` names, else to this board's model with the
/// owner's settings (its MCP servers stopped after); its events through `out`.
fn cli_question(q: &str, to: Option<&str>, out: &dyn Fn(agent::Event)) -> Result<(), String> {
    if let Some(name) = to {
        let (p, _) = prefs::Prefs::load(&prefs::Prefs::path());
        let o = p.online_named(name).ok_or_else(|| format!("no online assistant called \"{name}\" is switched on (the Assistant tab's Settings)"))?;
        let msgs = vec![json!({"role": "system", "content": online::system_prompt(o)}), json!({"role": "user", "content": q})];
        online::ask(o, &msgs, &AtomicBool::new(false), out);
        return Ok(());
    }
    let history = vec![json!({"role": "system", "content": agent::SYSTEM_PROMPT}), json!({"role": "user", "content": q})];
    let (tools, pool) = cli_tools();
    agent::run(history, tools, Arc::new(AtomicBool::new(false)), out);
    pool.stop_all(true); // (exit runs no destructors, nor waits for threads)
    Ok(())
}

/// `--ask`: one question through the same agent as the tab, printed as it happens.
fn ask_cli(q: &str, to: Option<&str>) -> ! {
    use std::io::Write;
    let failed = std::cell::Cell::new(false);
    let t0 = Instant::now();
    let r = cli_question(q, to, &|ev| match ev {
        agent::Event::Chunk(s) => {
            print!("{s}");
            let _ = std::io::stdout().flush();
        }
        agent::Event::Tool { audit, observation } => {
            println!("[tool {:.0} s] {audit}", t0.elapsed().as_secs_f32());
            for l in observation.lines().take(6) {
                println!("    {l}");
            }
        }
        agent::Event::Done { tok_s, tokens, .. } => println!("\n[done {:.0} s] {tok_s:.2} tokens/s, {tokens} tokens", t0.elapsed().as_secs_f32()),
        agent::Event::Fail(e) => {
            eprintln!("\n[failed {:.0} s] {e}", t0.elapsed().as_secs_f32());
            failed.set(true);
        }
        agent::Event::Note(n) => println!("[note] {n}"),
    });
    if let Err(e) = r {
        eprintln!("{e}");
        failed.set(true);
    }
    std::process::exit(if failed.get() { 1 } else { 0 })
}

/// `--answer`: one question through the same agent, the answer alone on stdout (a7a-voice ask shows it in a
/// notification: the Assistant by voice); a failure on stderr, exit 1.
fn answer_cli(q: &str, to: Option<&str>) -> ! {
    let result = std::cell::RefCell::new(Err("no answer from the model".to_string()));
    let r = cli_question(q, to, &|ev| match ev {
        agent::Event::Done { text, .. } => *result.borrow_mut() = Ok(text),
        agent::Event::Fail(e) => *result.borrow_mut() = Err(e),
        agent::Event::Note(n) => eprintln!("{n}"),
        _ => {}
    });
    if let Err(e) = r {
        *result.borrow_mut() = Err(e);
    }
    match result.into_inner() {
        Ok(t) if !t.trim().is_empty() => {
            println!("{}", t.trim());
            std::process::exit(0)
        }
        Ok(_) => {
            eprintln!("the model gave no answer");
            std::process::exit(1)
        }
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(1)
        }
    }
}

fn main() -> iced::Result {
    let args: Vec<String> = std::env::args().collect();
    // --assistant NAME: one of the online assistants (prefs::Online) answers instead of this board's model
    let to = args.iter().position(|a| a == "--assistant").and_then(|i| args.get(i + 1)).map(String::as_str);
    if let Some(q) = args.iter().position(|a| a == "--ask").and_then(|i| args.get(i + 1)) {
        ask_cli(q, to);
    }
    if let Some(q) = args.iter().position(|a| a == "--answer").and_then(|i| args.get(i + 1)) {
        answer_cli(q, to);
    }
    let demo = args.iter().any(|a| a == "--demo") || !sys::on_board();
    let screenshot = args.iter().position(|a| a == "--screenshot").and_then(|i| args.get(i + 1).cloned());
    let tab = match args.iter().position(|a| a == "--tab").and_then(|i| args.get(i + 1)).map(String::as_str) {
        Some("controls") => Tab::Controls,
        Some("hardware") => Tab::Hardware,
        Some("assistant" | "settings") => Tab::Assistant,
        Some("voice") => Tab::Voice,
        _ => Tab::Overview,
    };
    // --size WxH: the window's size (for a screenshot of a whole page); 1000x640 otherwise
    let size = args.iter().position(|a| a == "--size").and_then(|i| args.get(i + 1)).and_then(|v| v.split_once('x'))
        .and_then(|(w, h)| Some((w.parse::<f32>().ok()?, h.parse::<f32>().ok()?)))
        .filter(|(w, h)| (400.0..=4000.0).contains(w) && (300.0..=6000.0).contains(h))
        .unwrap_or((1000.0, 640.0));
    let guide = args.iter().position(|a| a == "--tab").and_then(|i| args.get(i + 1)).is_some_and(|t| t == "guide");
    let settings = args.iter().position(|a| a == "--tab").and_then(|i| args.get(i + 1)).is_some_and(|t| t == "settings");
    iced::application(
        move || {
            let (mut panel, task) = Panel::new(demo, screenshot.clone(), tab);
            if guide {
                // `--tab guide`: Getting started (in the demo: a board half set up)
                panel.guide_open = true;
                if demo {
                    panel.voice.ready = false;
                    panel.voice.enabled = false;
                    panel.models.clear();
                    panel.prefs.online.clear();
                    panel.jack = sys::Jack::Switch(sys::Codec::Ac101);
                }
            }
            if settings {
                // `--tab settings`: the Assistant's settings page (with an example in the demo)
                if demo {
                    panel.prefs.folders = vec!["/home/radxa/Documents".into(), "~/projects".into()];
                    panel.prefs.instructions = "Answer in two or three sentences.\nUse metric units.".into();
                    panel.prefs.mcp = vec![prefs::Server { name: "files".into(), command: "npx -y @modelcontextprotocol/server-filesystem /home/radxa/Documents".into(),
                                                           reach: prefs::Reach::Board, on: true, ..Default::default() }];
                    panel.prefs.online = vec![prefs::Online { name: "google".into(), provider: prefs::Provider::Google, on: true, ..Default::default() },
                                              prefs::Online { name: "brave".into(), provider: prefs::Provider::Brave, key: "demo".into(), on: true, ..Default::default() }];
                }
                let _ = panel.update(Message::SettingsOpen);
            }
            (panel, task)
        },
        Panel::update,
        Panel::view,
    )
        .title("A7A Panel")
        .theme(|_: &Panel| ui::theme())
        .subscription(Panel::subscription)
        .window_size(size)
        // the close button comes to update() as Message::Close, which stops the tab's reading first
        .exit_on_close_request(false)
        .run()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_online_assistant_is_shown_by_its_model_and_where_it_answers_from() {
        assert_eq!(short_model("@cf/openai/gpt-oss-120b"), "gpt-oss-120b");
        assert_eq!(short_model("mistral-small-latest"), "mistral-small-latest");
        let cf = prefs::Online {
            provider: prefs::Provider::Other,
            url: "https://api.cloudflare.com/client/v4/accounts/x/ai/v1".into(),
            ..Default::default()
        };
        assert_eq!(provider_place(&cf), "cloudflare.com");
        let local = prefs::Online { provider: prefs::Provider::Other, url: "http://192.168.1.10:8080/v1".into(), ..Default::default() };
        assert_eq!(provider_place(&local), "192.168.1.10:8080");
        let google = prefs::Online { provider: prefs::Provider::Google, ..Default::default() };
        assert_eq!(provider_place(&google), "Google");
    }
}
