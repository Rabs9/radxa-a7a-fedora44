//! What the panel reads from the board, straight from sysfs, procfs and the live device tree, without root: the GPU
//! runs the clock its boot entry's device tree sets (no DVFS), the rail's voltage is in /sys/class/regulator and the
//! DSU module's rate in its parameters. Only the GPU's recovery count needs root (DDK debugfs): `a7a-clock json`,
//! asked while the Hardware tab is open (every sudo is a journal entry, so not once a second). Every change goes
//! through `a7a-clock`, as the Qt panel did: the control logic lives in one place, and that place has its tests
//! (tests/test_clock.py).
//! Off the board (no A733 memory controller) the panel runs on generated values, so its look can be worked on
//! anywhere: `a7a-panel --demo` forces that.

use std::collections::HashMap;
use std::fs;
use std::path::Path;

pub const DMC: &str = "/sys/class/devfreq/a020000.dmcfreq";
pub const NPU: &str = "/sys/class/devfreq/3600000.npu";
pub const POL_LITTLE: &str = "/sys/devices/system/cpu/cpufreq/policy0";
pub const POL_BIG: &str = "/sys/devices/system/cpu/cpufreq/policy6";

fn read(path: impl AsRef<Path>) -> Option<String> {
    fs::read_to_string(path).ok().map(|s| s.trim().to_string())
}
fn read_u64(path: impl AsRef<Path>) -> Option<u64> {
    read(path)?.parse().ok()
}

/// One CPU cluster: clocks in MHz.
#[derive(Debug, Clone, Default)]
pub struct Cluster {
    pub cur: u32,
    pub cap: u32,
    pub top: u32,
    pub governor: String,
    pub governors: Vec<String>,
    pub steps: Vec<u32>,
    pub load: f32, // 0..1, the busiest core of the cluster
}

/// A devfreq device (memory controller, NPU): rates in MHz.
#[derive(Debug, Clone, Default)]
pub struct Rate {
    pub cur: u32,
    pub limit: u32,
    pub rates: Vec<u32>,
    pub governor: String,
}

#[derive(Debug, Clone, Default)]
pub struct Snapshot {
    pub little: Cluster,
    pub big: Cluster,
    pub ram: Rate,
    pub npu: Rate,
    pub temps: Vec<(String, f32)>,
    pub power: String,
    pub mem_total_mb: u64,
    pub mem_avail_mb: u64,
    pub uptime_s: u64,
}

/// What `a7a-clock json` reports (root-only files).
#[derive(Debug, Clone, Default)]
pub struct Privileged {
    pub gpu_mhz: Option<u32>,
    pub gpu_boot_mhz: Option<u32>,
    pub gpu_mv: Option<u32>,
    pub dsu_mhz: Option<u32>,
    pub dsu_module: String,
    /// the over-bin rung dsu_dyn was loaded with (its oc_hz), in MHz: "with the load + 1352"
    pub dsu_rung_mhz: Option<u32>,
    pub boot_entry: String,
}

#[derive(Debug, Clone, Default)]
pub struct Image {
    pub variant: String,
    pub kernel: String,
}

/// One running program, for the hardware tab.
#[derive(Debug, Clone)]
pub struct Process {
    pub pid: u32,
    pub name: String,
    pub cpu: f32,  // share of one core, 0..n
    pub rss_mb: u64,
    pub gpu: bool, // has the GPU's render node or the DDK's device open
    pub display: bool,
    pub npu: bool,
    pub mine: bool, // its open files could be read (the user's own programs)
}

pub fn on_board() -> bool {
    Path::new(DMC).exists() && Path::new(POL_BIG).exists()
}

pub fn image() -> Image {
    let variant = read("/etc/a7a-image-variant")
        .and_then(|s| s.lines().find_map(|l| l.strip_prefix("variant=").map(str::to_string)))
        .unwrap_or_else(|| "—".into());
    let kernel = read("/proc/sys/kernel/osrelease").unwrap_or_default();
    Image { variant, kernel }
}

/// Per-CPU busy counters from /proc/stat, for the cluster loads.
#[derive(Debug, Clone, Default)]
pub struct CpuTimes(HashMap<usize, (u64, u64)>); // cpu -> (busy, total)

pub fn cpu_times() -> CpuTimes {
    let mut m = HashMap::new();
    if let Some(s) = read("/proc/stat") {
        for l in s.lines() {
            let mut it = l.split_whitespace();
            let Some(name) = it.next() else { continue };
            let Some(n) = name.strip_prefix("cpu").and_then(|n| n.parse::<usize>().ok()) else { continue };
            let v: Vec<u64> = it.filter_map(|x| x.parse().ok()).collect();
            if v.len() >= 4 {
                let total: u64 = v.iter().sum();
                let idle = v[3] + v.get(4).copied().unwrap_or(0);
                m.insert(n, (total - idle, total));
            }
        }
    }
    CpuTimes(m)
}

fn related(policy: &str) -> Vec<usize> {
    read(format!("{policy}/related_cpus"))
        .map(|s| s.split_whitespace().filter_map(|x| x.parse().ok()).collect())
        .unwrap_or_default()
}

fn cluster(policy: &str, prev: &CpuTimes, now: &CpuTimes) -> Cluster {
    let khz = |f: &str| read_u64(format!("{policy}/{f}")).map(|v| (v / 1000) as u32).unwrap_or(0);
    let mut top = khz("cpuinfo_max_freq");
    if policy == POL_BIG {
        top = top.min(3000); // the table may list 3.2 GHz; the PLL gives 3.0 (docs/cpu-oc-findings.md)
    }
    let load = related(policy)
        .iter()
        .filter_map(|c| {
            let (b0, t0) = prev.0.get(c)?;
            let (b1, t1) = now.0.get(c)?;
            (t1 > t0).then(|| (b1 - b0) as f32 / (t1 - t0) as f32)
        })
        .fold(0.0f32, f32::max);
    Cluster {
        cur: khz("scaling_cur_freq"),
        cap: khz("scaling_max_freq"),
        top,
        governor: read(format!("{policy}/scaling_governor")).unwrap_or_default(),
        governors: read(format!("{policy}/scaling_available_governors"))
            .map(|s| s.split_whitespace().map(str::to_string).collect())
            .unwrap_or_default(),
        steps: read(format!("{policy}/scaling_available_frequencies"))
            .map(|s| s.split_whitespace().filter_map(|x| x.parse::<u64>().ok()).map(|v| (v / 1000) as u32).collect())
            .unwrap_or_default(),
        load,
    }
}

fn rate(dev: &str) -> Rate {
    let mhz = |f: &str| read_u64(format!("{dev}/{f}")).map(|v| (v / 1_000_000) as u32).unwrap_or(0);
    // the NPU's cur_freq is its module clock and never moves: the rate in effect is devfreq's target
    let cur = if dev == NPU { mhz("target_freq").max(0) } else { mhz("cur_freq") };
    let mut rates: Vec<u32> = read(format!("{dev}/available_frequencies"))
        .map(|s| s.split_whitespace().filter_map(|x| x.parse::<u64>().ok()).map(|v| (v / 1_000_000) as u32).collect())
        .unwrap_or_default();
    rates.sort_unstable();
    Rate { cur: if cur == 0 { mhz("cur_freq") } else { cur }, limit: mhz("max_freq"), rates, governor: read(format!("{dev}/governor")).unwrap_or_default() }
}

pub fn temps() -> Vec<(String, f32)> {
    let mut v = Vec::new();
    if let Ok(dir) = fs::read_dir("/sys/class/thermal") {
        let mut zones: Vec<_> = dir.flatten().filter(|e| e.file_name().to_string_lossy().starts_with("thermal_zone")).collect();
        zones.sort_by_key(|e| e.file_name());
        for z in zones {
            let p = z.path();
            if let (Some(name), Some(t)) = (read(p.join("type")), read_u64(p.join("temp"))) {
                let name = name.trim_end_matches("_thermal_zone").trim_end_matches("_zone").to_string();
                v.push((name, t as f32 / 1000.0));
            }
        }
    }
    v
}

/// dsu_dyn's oc_hz parameter as a rung in MHz; 0 (no rung) and an older module without the parameter are None
fn rung_mhz(oc_hz: Option<String>) -> Option<u32> {
    oc_hz.and_then(|v| v.trim().parse::<u64>().ok()).filter(|hz| *hz > 0).map(|hz| (hz / 1_000_000) as u32)
}

/// The power profiles: the key of tuned-ppd's ActiveProfile (what KDE's slider sets), the a7a tuned profile the
/// panel's buttons switch tuned to, and the name the panel shows.
const PROFILES: [(&str, &str, &str); 3] = [
    ("power-saver", "a7a-powersave", "Power Save"),
    ("balanced", "a7a-balanced", "Balanced"),
    ("performance", "a7a-performance", "Performance"),
];

/// The name power_profile() shows for a button's key ("" for none).
pub fn power_name(key: &str) -> &'static str {
    PROFILES.iter().find(|p| p.0 == key).map_or("", |p| p.2)
}

fn tuned_profile(key: &str) -> Option<&'static str> {
    PROFILES.iter().find(|p| p.0 == key).map(|p| p.1)
}

pub fn power_profile() -> String {
    profile_shown(read("/etc/tuned/active_profile").as_deref())
}

/// tuned's active profile as the panel shows it
fn profile_shown(active: Option<&str>) -> String {
    match active {
        Some(t) if !t.is_empty() => PROFILES.iter().find(|p| p.1 == t).map_or_else(|| t.to_string(), |p| p.2.to_string()),
        _ => "—".into(),
    }
}

pub fn snapshot(prev: &CpuTimes, now: &CpuTimes) -> Snapshot {
    let mut mem_total_mb = 0;
    let mut mem_avail_mb = 0;
    if let Some(s) = read("/proc/meminfo") {
        for l in s.lines() {
            let kb = |l: &str| l.split_whitespace().nth(1).and_then(|x| x.parse::<u64>().ok()).unwrap_or(0) / 1024;
            if l.starts_with("MemTotal:") {
                mem_total_mb = kb(l);
            } else if l.starts_with("MemAvailable:") {
                mem_avail_mb = kb(l);
            }
        }
    }
    Snapshot {
        little: cluster(POL_LITTLE, prev, now),
        big: cluster(POL_BIG, prev, now),
        ram: rate(DMC),
        npu: rate(NPU),
        temps: temps(),
        power: power_profile(),
        mem_total_mb,
        mem_avail_mb,
        uptime_s: read("/proc/uptime").and_then(|s| s.split('.').next()?.parse().ok()).unwrap_or(0),
    }
}

pub const GPU_DT: &str = "/proc/device-tree/soc@3000000/gpu@1800000/clk_rate";

/// A big-endian u32 device-tree property.
fn dt_u32(path: &str) -> Option<u32> {
    let b = fs::read(path).ok()?;
    (b.len() >= 4).then(|| u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
}

/// The GPU's clock and rail, the L3 and the boot entry, from what a user may read.
pub fn privileged() -> Privileged {
    let mut p = Privileged::default();
    p.gpu_boot_mhz = dt_u32(GPU_DT).map(|hz| hz / 1_000_000);
    p.gpu_mhz = p.gpu_boot_mhz;
    if let Ok(dir) = fs::read_dir("/sys/class/regulator") {
        for e in dir.flatten() {
            if read(e.path().join("name")).is_some_and(|n| n.ends_with("dcdc4")) {
                p.gpu_mv = read(e.path().join("microvolts")).and_then(|v| v.parse::<u32>().ok()).map(|uv| uv / 1000);
            }
        }
    }
    let modules = read("/proc/modules").unwrap_or_default();
    let loaded = |m: &str| modules.lines().any(|l| l.split(' ').next() == Some(m));
    if loaded("dsu_oc") {
        p.dsu_module = "dsu_oc".into();
        p.dsu_mhz = read("/sys/module/dsu_oc/parameters/freq").and_then(|v| v.parse::<u64>().ok()).map(|hz| (hz / 1_000_000) as u32);
    } else if loaded("dsu_dyn") {
        p.dsu_module = "dsu_dyn".into(); // the rate follows the load: no single number
        p.dsu_rung_mhz = rung_mhz(read("/sys/module/dsu_dyn/parameters/oc_hz"));
    } else {
        p.dsu_module = "none".into();
        p.dsu_mhz = Some(780);
    }
    p.boot_entry = read("/boot/extlinux/extlinux.conf")
        .and_then(|c| c.lines().find_map(|l| l.strip_prefix("default").map(|r| r.trim().to_string())))
        .unwrap_or_default();
    p
}

/// What the GPU driver says about itself (its debugfs, root only): load in percent overall and per unit, the memory
/// its programs hold, the hardware recoveries of this boot. All None without the driver or without sudo.
#[derive(Debug, Clone, Default)]
pub struct GpuStat {
    pub load: Option<u32>,
    pub load_3d: Option<u32>,
    pub load_geom: Option<u32>,
    pub load_compute: Option<u32>,
    pub mem_mb: Option<u32>,
    pub hwr: Option<u32>,
}

/// The 3.5 mm jack's codec (2026-11-24). Radxa builds the board with an AC101 or an AC101B, and the packaged device
/// tree describes the AC101B only: on an AC101 board the jack has no sound card until `a7a-clock codec ac101` and a
/// restart. `a7a-clock codec state` says which case this board is in, from its device trees, its sound cards and
/// this boot's kernel log ("try read ac101b 5 times but failed").
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Codec {
    Ac101,
    Ac101b,
}

impl Codec {
    /// a7a-clock's word for it (`a7a-clock codec <arg>`)
    pub fn arg(self) -> &'static str {
        match self {
            Codec::Ac101 => "ac101",
            Codec::Ac101b => "ac101b",
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Codec::Ac101 => "AC101",
            Codec::Ac101b => "AC101B",
        }
    }

    pub fn other(self) -> Codec {
        match self {
            Codec::Ac101 => Codec::Ac101b,
            Codec::Ac101b => Codec::Ac101,
        }
    }

    fn parse(s: &str) -> Option<Codec> {
        match s {
            "AC101" => Some(Codec::Ac101),
            "AC101B" => Some(Codec::Ac101b),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum Jack {
    /// not asked yet
    #[default]
    Unknown,
    /// a7a-clock said something else (one from before `codec state`, or not there): its first line
    Unreadable(String),
    /// the jack's sound card is up, on that codec
    Ok(Codec),
    /// the jack's card is up on that codec, but the trees were switched to the other: a restart would silence it
    Keep(Codec),
    /// the trees describe that codec, the running one the other: a restart applies the switch
    Reboot(Codec),
    /// no card, and the running tree's codec did not answer at boot: this one is on the board
    Switch(Codec),
    /// no card, and nothing in this boot's log to go by
    NoCard,
}

impl Jack {
    /// the codec its button switches to: only when the board said which one it is built with, or which one works
    pub fn fix(&self) -> Option<Codec> {
        match self {
            Jack::Switch(c) | Jack::Keep(c) => Some(*c),
            _ => None,
        }
    }

    /// whether something is to be done (the guide shows the jack then)
    pub fn wants_action(&self) -> bool {
        matches!(self, Jack::Switch(_) | Jack::Reboot(_) | Jack::Keep(_))
    }

    /// its button's label
    pub fn fix_label(&self) -> &'static str {
        match self {
            Jack::Keep(Codec::Ac101) => "Keep the AC101",
            Jack::Keep(Codec::Ac101b) => "Keep the AC101B",
            Jack::Switch(Codec::Ac101b) => "Back to the AC101B",
            _ => "Switch to the AC101",
        }
    }

    pub fn words(&self) -> String {
        match self {
            Jack::Unknown => "Asking the board\u{2026}".into(),
            Jack::Unreadable(s) if s.is_empty() => "a7a-clock gave no answer about the jack.".into(),
            Jack::Unreadable(s) => format!("a7a-clock answered: {s}"),
            Jack::Ok(Codec::Ac101) => "Working, on this board's AC101 chip (the device trees were switched to it).".into(),
            Jack::Ok(Codec::Ac101b) => "Working, on this board's AC101B chip.".into(),
            Jack::Keep(c) => format!(
                "The jack works now, on the {}, but the device trees were switched to the {}: a restart would silence it. Keep the {} unless you know this board is built with the other chip.",
                c.name(),
                c.other().name(),
                c.name()
            ),
            Jack::Reboot(c) => format!("The device trees now describe the {}: restart the board to apply it.", c.name()),
            Jack::Switch(Codec::Ac101) => "No sound from the jack: the AC101B did not answer at boot, so this board is built with the AC101 chip, which the stock device tree does not describe. Switch to the AC101, then restart.".into(),
            Jack::Switch(Codec::Ac101b) => "No sound from the jack: the AC101 did not answer at boot, so this board is built with the AC101B chip. Switch back to it, then restart.".into(),
            Jack::NoCard => "The jack has no sound card, and this boot's log does not say why.".into(),
        }
    }
}

pub fn parse_jack(out: &str) -> Jack {
    let line = out.lines().next().unwrap_or("").trim();
    let words: Vec<&str> = line.split_whitespace().collect();
    let jack = match words.as_slice() {
        ["ok", c] => Codec::parse(c).map(Jack::Ok),
        ["keep", c] => Codec::parse(c).map(Jack::Keep),
        ["reboot", c] => Codec::parse(c).map(Jack::Reboot),
        ["switch", c] => Codec::parse(c).map(Jack::Switch),
        ["none"] => Some(Jack::NoCard),
        _ => None,
    };
    jack.unwrap_or_else(|| Jack::Unreadable(line.to_string()))
}

/// The longest line the panel's log keeps (its Output box and status line lay text out at every redraw).
pub const LOG_LINE_MAX: usize = 300;

/// A command's output as the log keeps it, line by line: of a line that redraws itself with carriage returns (curl's
/// progress bar: tens of thousands of states in one "line"), what a terminal ends up showing, its last state; empty
/// lines dropped; each line cut at LOG_LINE_MAX characters (2026-11-24, the first board of the image: the model
/// download's bar filled the Voice tab with hundreds of lines and the panel froze laying them out).
pub fn log_lines(s: &str) -> Vec<String> {
    s.lines()
        .filter_map(|l| l.split('\r').map(str::trim_end).filter(|seg| !seg.trim().is_empty()).last())
        .map(|l| match l.char_indices().nth(LOG_LINE_MAX) {
            Some((i, _)) => format!("{}\u{2026}", &l[..i]),
            None => l.to_string(),
        })
        .collect()
}

fn exit_line(l: &str) -> bool {
    l.starts_with("(exit ") && l.ends_with(')')
}

/// The last line a command said, for a note in the window: not curl's progress bar ('#'), not printed()'s "(exit N)"
/// (2026-11-24: a failed model download said "The download did not finish: (exit 1)").
pub fn last_said(out: &str) -> Option<&str> {
    out.lines().map(str::trim).filter(|l| !l.is_empty() && !l.contains('#') && !exit_line(l)).last()
}

/// What a failed command's output says, for a line beside its button: its last lines but a7a-clock's "(see above)"
/// pointers and `printed`'s "(exit N)" (review 2026-11-24: the guide said "The switch stopped: (exit 1)").
pub fn why_failed(out: &str) -> String {
    let lines: Vec<&str> = out
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('$') && !exit_line(l))
        .collect();
    let said: Vec<&str> = lines.iter().copied().filter(|l| !l.contains("(above)")).collect();
    let pick = if said.is_empty() { &lines } else { &said };
    pick[pick.len().saturating_sub(2)..].join(" ")
}

/// `a7a-clock codec state`: no terminal, so never a password prompt (it reads the log through `sudo -n` only).
pub fn jack() -> Jack {
    match std::process::Command::new("a7a-clock").args(["codec", "state"]).stdin(std::process::Stdio::null()).output() {
        Ok(out) => parse_jack(&String::from_utf8_lossy(&out.stdout)),
        Err(e) => Jack::Unreadable(format!("a7a-clock could not be started: {e}")),
    }
}

/// `a7a-clock json` (a7a-gpu-stat through sudo; the image keeps those runs out of the journal).
pub fn gpu_stat() -> GpuStat {
    let Ok(out) = std::process::Command::new("a7a-clock").arg("json").stdin(std::process::Stdio::null()).output() else {
        return GpuStat::default();
    };
    parse_gpu_stat(&String::from_utf8_lossy(&out.stdout))
}

pub fn parse_gpu_stat(text: &str) -> GpuStat {
    let mut g = GpuStat::default();
    for l in text.lines() {
        let Some((k, v)) = l.split_once('=') else { continue };
        let n = v.trim().parse::<u32>().ok();
        match k.trim() {
            "gpu_load" => g.load = n,
            "gpu_3d" => g.load_3d = n,
            "gpu_geom" => g.load_geom = n,
            "gpu_cdm" => g.load_compute = n,
            "gpu_mem_mb" => g.mem_mb = n,
            "hwr" => g.hwr = n,
            _ => {}
        }
    }
    g
}

/// Per-process CPU ticks, for the processes' CPU share between two reads.
pub type ProcTicks = HashMap<u32, u64>;

pub fn processes(prev: &ProcTicks, elapsed_s: f32) -> (Vec<Process>, ProcTicks) {
    let hz = 100.0; // USER_HZ on arm64 Linux
    let me = unsafe_uid();
    let mut out = Vec::new();
    let mut ticks = HashMap::new();
    let Ok(dir) = fs::read_dir("/proc") else { return (out, ticks) };
    for e in dir.flatten() {
        let Ok(pid) = e.file_name().to_string_lossy().parse::<u32>() else { continue };
        let base = e.path();
        let Some(stat) = read(base.join("stat")) else { continue };
        // the name is in parentheses and may hold spaces: the fields after it count from the last ')'
        let Some(close) = stat.rfind(')') else { continue };
        let name = stat[stat.find('(').map(|i| i + 1).unwrap_or(0)..close].to_string();
        let f: Vec<&str> = stat[close + 2..].split_whitespace().collect();
        if f.len() < 22 {
            continue;
        }
        let t = f[11].parse::<u64>().unwrap_or(0) + f[12].parse::<u64>().unwrap_or(0);
        let rss_mb = f[21].parse::<u64>().unwrap_or(0) * 4096 / 1_048_576;
        ticks.insert(pid, t);
        let cpu = prev.get(&pid).map(|p0| (t.saturating_sub(*p0)) as f32 / hz / elapsed_s.max(0.1)).unwrap_or(0.0);
        let owner = fs::metadata(&base).ok().map(|m| std::os::unix::fs::MetadataExt::uid(&m));
        let mine = owner == Some(me);
        let (mut gpu, mut display, mut npu) = (false, false, false);
        if let Ok(fds) = fs::read_dir(base.join("fd")) {
            for fd in fds.flatten() {
                if let Ok(target) = fs::read_link(fd.path()) {
                    let t = target.to_string_lossy();
                    if t == "/dev/dri/renderD128" || t.starts_with("/dev/pvr") {
                        gpu = true;
                    } else if t.starts_with("/dev/dri/card") {
                        display = true;
                    } else if t == "/dev/vipcore" {
                        npu = true;
                    }
                }
            }
        }
        if rss_mb == 0 && cpu == 0.0 && !gpu && !npu {
            continue; // kernel threads and idle helpers
        }
        out.push(Process { pid, name, cpu, rss_mb, gpu, display, npu, mine });
    }
    out.sort_by(|a, b| b.cpu.partial_cmp(&a.cpu).unwrap_or(std::cmp::Ordering::Equal).then(b.rss_mb.cmp(&a.rss_mb)));
    (out, ticks)
}

fn unsafe_uid() -> u32 {
    fs::metadata("/proc/self").ok().map(|m| std::os::unix::fs::MetadataExt::uid(&m)).unwrap_or(u32::MAX)
}

/// Runs `a7a-clock <args>` and returns what it printed, for the panel's log.
pub fn a7a_clock(args: Vec<String>) -> String {
    command("a7a-clock", &args)
}

/// Runs a program and returns what it printed (stdout, then stderr, then the exit code when it failed).
pub fn command<S: AsRef<std::ffi::OsStr>>(program: &str, args: &[S]) -> String {
    match std::process::Command::new(program).args(args).output() {
        Ok(o) => printed(&o),
        Err(e) => format!("{program} could not be started: {e}"),
    }
}

/// `command`, with `input` on the program's stdin (closed after it, so the program sees the end of it).
pub fn command_with_input<S: AsRef<std::ffi::OsStr>>(program: &str, args: &[S], input: &str) -> String {
    use std::io::Write;
    use std::process::Stdio;
    match std::process::Command::new(program).args(args).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn() {
        Ok(mut child) => {
            if let Some(mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(input.as_bytes());
            }
            match child.wait_with_output() {
                Ok(o) => printed(&o),
                Err(e) => format!("{program}: {e}"),
            }
        }
        Err(e) => format!("{program} could not be started: {e}"),
    }
}

fn printed(o: &std::process::Output) -> String {
    let mut s = String::from_utf8_lossy(&o.stdout).trim_end().to_string();
    let e = String::from_utf8_lossy(&o.stderr);
    if !e.trim().is_empty() {
        s.push('\n');
        s.push_str(e.trim_end());
    }
    if !o.status.success() {
        s.push_str(&format!("\n(exit {})", o.status.code().unwrap_or(-1)));
    }
    s
}

/// The WiFi link as it is now, and the band the saved network is locked to ("bg", "a" or "" for both).
#[derive(Debug, Clone, Default)]
pub struct Wifi {
    pub freq_mhz: Option<u32>,
    pub signal_dbm: Option<i32>,
    pub rate_mbit: Option<f32>,
    pub lock: Option<String>,
}

impl Wifi {
    /// "2.4 GHz channel 1" / "5 GHz channel 161"
    pub fn place(&self) -> Option<String> {
        let f = self.freq_mhz?;
        Some(match f {
            2412..=2472 => format!("2.4 GHz channel {}", (f - 2407) / 5),
            2484 => "2.4 GHz channel 14".into(),
            5000..=5900 => format!("5 GHz channel {}", (f - 5000) / 5),
            _ => format!("{f} MHz"),
        })
    }
}

/// `iw dev <wifi> link` and the active profile's band, both readable without root.
pub fn wifi() -> Wifi {
    let mut w = Wifi::default();
    let out = |cmd: &str, args: &[&str]| {
        std::process::Command::new(cmd).args(args).output().ok().map(|o| String::from_utf8_lossy(&o.stdout).into_owned()).unwrap_or_default()
    };
    let dev = out("nmcli", &["-t", "-f", "DEVICE,TYPE", "dev", "status"])
        .lines()
        .find_map(|l| l.strip_suffix(":wifi").map(str::to_string))
        .unwrap_or_else(|| "wlan0".into());
    parse_iw_link(&out("iw", &["dev", &dev, "link"]), &mut w);
    if let Some(uuid) = out("nmcli", &["-t", "-f", "UUID,TYPE", "con", "show", "--active"])
        .lines()
        .find_map(|l| l.strip_suffix(":802-11-wireless").map(str::to_string))
    {
        w.lock = Some(out("nmcli", &["-g", "802-11-wireless.band", "con", "show", &uuid]).trim().to_string());
    }
    w
}

pub fn parse_iw_link(text: &str, w: &mut Wifi) {
    for l in text.lines().map(str::trim) {
        let num = |s: &str| s.split_whitespace().next().and_then(|v| v.parse::<f32>().ok());
        if let Some(v) = l.strip_prefix("freq:") {
            w.freq_mhz = num(v).map(|f| f as u32);
        } else if let Some(v) = l.strip_prefix("signal:") {
            w.signal_dbm = num(v).map(|f| f as i32);
        } else if let Some(v) = l.strip_prefix("tx bitrate:") {
            w.rate_mbit = num(v);
        }
    }
}

/// A power profile, through tuned itself (com.redhat.tuned switch_profile, the profile of KDE's slider position).
/// tuned runs the profile's script on every switch, also to the profile it is on, so a press puts back the clocks
/// changed below; tuned-ppd's ActiveProfile, which the slider sets, takes a request for the profile already set as done
/// and runs nothing (on the board 2026-11-17, Balanced pressed after the dynoc preset kept the L3's 1352 MHz rung).
/// tuned-ppd follows tuned's switch, so the slider shows it. The call returns once tuned has started the switch (no
/// tuned-adm: it can wait 600 s for a signal, and falls back to restarting tuned behind a password dialog).
pub fn set_power(key: &str) -> String {
    let Some(t) = tuned_profile(key) else { return format!("no a7a profile for {key}") };
    let out = command("busctl", &["--system", "call", "com.redhat.tuned", "/Tuned", "com.redhat.tuned.control", "switch_profile", "s", t]);
    if !switch_ok(out.lines().next().unwrap_or("")) {
        return format!("power profile {key}: {}", out.trim());
    }
    // tuned-ppd's ActiveProfile as well, as the slider sets it: tuned is on that profile already, so nothing is
    // switched again, but a hold an application has put on a profile (GameMode) ends, and the press is kept as the
    // profile tuned-ppd goes back to when a hold ends
    let ppd = command("busctl", &["--system", "set-property", "org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles",
                                  "org.freedesktop.UPower.PowerProfiles", "ActiveProfile", "s", key]);
    if ppd.trim().is_empty() { format!("power profile -> {key}") } else { format!("power profile -> {key} (tuned-ppd: {})", ppd.trim()) }
}

/// switch_profile's answer as busctl prints it: `(bs) true "OK"`, or `(bs) false "Unauthorized"` with busctl
/// exiting 0 all the same (outside the desktop session, polkit says no)
fn switch_ok(reply: &str) -> bool {
    reply.split_whitespace().nth(1) == Some("true")
}

/// Generated values for working on the look off the board.
pub mod demo {
    use super::*;

    pub fn snapshot(t: f32) -> Snapshot {
        let wave = |p: f32, a: f32, b: f32| a + (b - a) * (0.5 + 0.5 * (t * p).sin());
        Snapshot {
            little: Cluster { cur: wave(0.7, 416.0, 2800.0) as u32 / 100 * 100, cap: 2800, top: 2800, governor: "schedutil".into(),
                              governors: vec!["schedutil".into(), "performance".into(), "powersave".into(), "ondemand".into()],
                              steps: vec![416, 1014, 1196, 1794, 2000, 2400, 2800], load: wave(0.7, 0.05, 0.9) },
            big: Cluster { cur: wave(0.45, 780.0, 3000.0) as u32 / 100 * 100, cap: 3000, top: 3000, governor: "schedutil".into(),
                           governors: vec!["schedutil".into(), "performance".into()], steps: vec![408, 1014, 1508, 2200, 2600, 3000],
                           load: wave(0.45, 0.02, 0.97) },
            ram: Rate { cur: 2136, limit: 2136, rates: vec![427, 854, 1220, 2136], governor: "performance".into() },
            npu: Rate { cur: 1008, limit: 1008, rates: vec![492, 852, 1008], governor: "performance".into() },
            temps: vec![("cpub".into(), wave(0.2, 38.0, 64.0)), ("cpul".into(), wave(0.25, 37.0, 55.0)),
                        ("gpu".into(), wave(0.3, 36.0, 58.0)), ("npu".into(), 36.0), ("ddr".into(), wave(0.1, 35.0, 44.0))],
            power: "Balanced".into(),
            mem_total_mb: 11928,
            mem_avail_mb: wave(0.05, 6200.0, 9600.0) as u64,
            uptime_s: 4321 + t as u64,
        }
    }

    pub fn privileged(t: f32) -> Privileged {
        Privileged { gpu_mhz: Some(1008), gpu_boot_mhz: Some(1008), gpu_mv: Some(800),
                     dsu_mhz: Some(if (t * 0.4).sin() > 0.0 { 1274 } else { 780 }), dsu_module: "dsu_dyn".into(),
                     dsu_rung_mhz: None, boot_entry: "fedora-6.6.98-a7a.202609091931".into() }
    }

    pub fn gpu(t: f32) -> GpuStat {
        let load = (38.0 + 30.0 * (t * 0.5).sin() + 8.0 * (t * 1.7).sin()).clamp(0.0, 100.0) as u32;
        GpuStat { load: Some(load), load_3d: Some(load.saturating_sub(4)), load_geom: Some(load / 3), load_compute: Some(0),
                  mem_mb: Some((420.0 + 300.0 * (0.5 + 0.5 * (t * 0.3).sin())) as u32), hwr: Some(0) }
    }

    pub fn processes(t: f32) -> Vec<Process> {
        let p = |pid, name: &str, cpu: f32, rss, gpu, display, npu| Process { pid, name: name.into(), cpu, rss_mb: rss, gpu, display, npu, mine: true };
        vec![
            p(2015, "kwin_wayland", 0.18 + 0.1 * (t * 0.9).sin().abs(), 181, true, true, false),
            p(2194, "plasmashell", 0.07, 1137, true, false, false),
            p(3121, "supertuxkart", 1.35 + 0.3 * (t * 0.6).sin().abs(), 812, true, false, false),
            p(1815, "Xwayland", 0.02, 96, true, false, false),
            p(4410, "a7a-panel", 0.04, 61, true, false, false),
            p(5120, "npu-run", 0.11, 140, false, false, true),
            p(980, "pipewire", 0.01, 18, false, false, false),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_wifi_link_as_iw_prints_it() {
        let mut w = Wifi::default();
        parse_iw_link("Connected to 02:11:22:33:44:55 (on wlan0)\n\tSSID: home\n\tfreq: 5805.0\n\tsignal: -72 dBm\n\trx bitrate: 68.0 MBit/s 80MHz HE-MCS 1\n\ttx bitrate: 72.0 MBit/s 80MHz HE-MCS 1 HE-NSS 1\n", &mut w);
        assert_eq!((w.freq_mhz, w.signal_dbm, w.rate_mbit), (Some(5805), Some(-72), Some(72.0)));
        assert_eq!(w.place().as_deref(), Some("5 GHz channel 161"));
        w.freq_mhz = Some(2412);
        assert_eq!(w.place().as_deref(), Some("2.4 GHz channel 1"));
        let mut off = Wifi::default();
        parse_iw_link("Not connected.\n", &mut off);
        assert!(off.freq_mhz.is_none() && off.place().is_none());
    }

    #[test]
    fn gpu_stat_lines_as_a7a_gpu_stat_prints_them() {
        let g = parse_gpu_stat("hwr=3\ngpu_load=37\ngpu_geom=12\ngpu_3d=35\ngpu_cdm=4\ngpu_mem_mb=330\n");
        assert_eq!((g.load, g.load_3d, g.load_geom, g.load_compute, g.mem_mb, g.hwr),
                   (Some(37), Some(35), Some(12), Some(4), Some(330), Some(3)));
        // no driver, no sudo: nothing printed, nothing known
        let g = parse_gpu_stat("");
        assert!(g.load.is_none() && g.hwr.is_none() && g.mem_mb.is_none());
    }

    #[test]
    fn the_power_buttons_keys_name_the_profiles_the_panel_reads_back() {
        // a button's key picks the profile tuned switches to; its name labels and lights the button and picks the
        // Overview's texts, which are written for these names
        for (key, tuned, shown) in [("power-saver", "a7a-powersave", "Power Save"), ("balanced", "a7a-balanced", "Balanced"),
                                    ("performance", "a7a-performance", "Performance")] {
            assert_eq!(power_name(key), shown, "{key}: the button's label (the Overview's texts are written for it)");
            assert_eq!(profile_shown(Some(tuned)), shown, "{key}: what the panel reads back");
            assert_eq!(tuned_profile(key), Some(tuned), "{key}: the profile tuned is asked for");
        }
        assert_eq!(profile_shown(Some("balanced")), "balanced", "a profile that is not an a7a one is shown as it is");
        assert_eq!(profile_shown(Some("")), "—");
        assert_eq!(power_name("something-else"), "");
        assert_ne!(profile_shown(None), power_name(""), "no tuned profile must not match the empty name");
    }

    #[test]
    fn tuneds_answer_to_a_switch_as_busctl_prints_it() {
        assert!(switch_ok("(bs) true \"OK\""));
        assert!(!switch_ok("(bs) false \"Unauthorized\""), "busctl exits 0 on a refusal: only the answer tells");
        assert!(!switch_ok(""));
    }

    #[test]
    fn the_jack_as_a7a_clock_codec_state_prints_it() {
        assert_eq!(parse_jack("ok AC101\n"), Jack::Ok(Codec::Ac101));
        assert_eq!(parse_jack("ok AC101B\n"), Jack::Ok(Codec::Ac101b));
        assert_eq!(parse_jack("keep AC101\n"), Jack::Keep(Codec::Ac101));
        assert_eq!(parse_jack("reboot AC101\n"), Jack::Reboot(Codec::Ac101));
        assert_eq!(parse_jack("switch AC101\n"), Jack::Switch(Codec::Ac101));
        assert_eq!(parse_jack("switch AC101B\n"), Jack::Switch(Codec::Ac101b));
        assert_eq!(parse_jack("none\n"), Jack::NoCard);
        // an a7a-clock from before `codec state` prints its usage: nothing is offered from that
        let old = "usage: a7a-clock codec ac101 [--force]|ac101b|status\n";
        assert_eq!(parse_jack(old), Jack::Unreadable(old.trim().into()));
        for bad in ["", "switch", "switch AC102", "none AC101", "ok AC101 extra", "Switch AC101"] {
            assert!(matches!(parse_jack(bad), Jack::Unreadable(_)), "{bad:?}");
        }
    }

    #[test]
    fn a_progress_bar_is_kept_as_its_last_state_and_long_lines_are_cut() {
        // curl --progress-bar: one "line" of many states separated by carriage returns
        let bar: String = (0..20000).map(|i| format!("\r{} {:.1}%", "#".repeat(i % 70), i as f32 / 200.0)).collect();
        let out = format!("downloading Qwen3 (2265 MB) into /home/radxa/models\n#=#=#\r##O#-#{bar}\r{} 100.0%\nchecking the file...\n\nready: x\n", "#".repeat(72));
        let kept = log_lines(&out);
        assert_eq!(kept.len(), 4, "{kept:?}");
        assert_eq!(kept[0], "downloading Qwen3 (2265 MB) into /home/radxa/models");
        assert!(kept[1].ends_with(" 100.0%") && kept[1].len() < 100, "{}", &kept[1][..60.min(kept[1].len())]);
        assert_eq!(&kept[2..], ["checking the file...", "ready: x"]);
        // a line longer than the cap: cut, on a character boundary
        let long = "\u{e9}".repeat(LOG_LINE_MAX + 50);
        let cut = &log_lines(&long)[0];
        assert_eq!(cut.chars().count(), LOG_LINE_MAX + 1);
        assert!(cut.ends_with('\u{2026}'));
        // a line that ends with a carriage return keeps what came before it; blank ones go
        assert_eq!(log_lines("50%\r\n \r \nok"), ["50%", "ok"]);
    }

    #[test]
    fn a_failed_download_says_what_a7a_llm_said_last() {
        let out = "downloading Qwen3-4B-Instruct-2507-Q4_0.gguf (2265 MB) into /home/radxa/models\ncurl: (6) Could not resolve host: huggingface.co\nno connection to huggingface.co (curl 6). Is the board on the internet? Connect, then download again: it carries on where it stopped\n(exit 1)";
        assert!(last_said(out).unwrap().starts_with("no connection to huggingface.co"));
        assert_eq!(last_said("##########    14.2%\n(exit 1)"), None);
        assert_eq!(last_said(""), None);
    }

    #[test]
    fn a_failed_switch_says_why_not_its_exit_code() {
        let out = "  /usr/lib/linux-image-6.6.99/allwinner/sun60i-a733-cubie-a7a.dtb: a7a-ac101-dtb: no ac101 node\ncodec: stopped (above); the choice is as it was\n(exit 1)";
        assert_eq!(why_failed(out), "/usr/lib/linux-image-6.6.99/allwinner/sun60i-a733-cubie-a7a.dtb: a7a-ac101-dtb: no ac101 node");
        let sudo = "sudo: a terminal is required to read the password; either use the -S option to read from standard input or configure an askpass helper\nsudo: a password is required\n(exit 1)";
        assert!(why_failed(sudo).contains("a password is required"));
        assert_eq!(why_failed("(exit 1)"), "");
        assert_eq!(why_failed("codec: stopped (above)\n(exit 1)"), "codec: stopped (above)");
    }

    #[test]
    fn only_the_switch_offers_a_button_and_it_names_a7a_clocks_word() {
        assert_eq!(Jack::Switch(Codec::Ac101).fix(), Some(Codec::Ac101));
        // a working jack with a switch pending: its button keeps the codec that works, and says so
        assert_eq!(Jack::Keep(Codec::Ac101b).fix(), Some(Codec::Ac101b));
        assert_eq!(Jack::Keep(Codec::Ac101b).fix_label(), "Keep the AC101B");
        assert!(Jack::Keep(Codec::Ac101).wants_action() && Jack::Keep(Codec::Ac101).words().contains("works now"));
        assert_eq!(Jack::Switch(Codec::Ac101).fix_label(), "Switch to the AC101");
        assert_eq!(Jack::Switch(Codec::Ac101b).fix_label(), "Back to the AC101B");
        assert_eq!(Codec::Ac101.arg(), "ac101");
        assert_eq!(Codec::Ac101b.arg(), "ac101b");
        for j in [Jack::Unknown, Jack::Ok(Codec::Ac101), Jack::Reboot(Codec::Ac101), Jack::NoCard, Jack::Unreadable(String::new())] {
            assert_eq!(j.fix(), None, "{j:?}");
        }
        // the guide speaks up only when something is to be done
        assert!(Jack::Switch(Codec::Ac101b).wants_action() && Jack::Reboot(Codec::Ac101).wants_action());
        assert!(!Jack::Ok(Codec::Ac101).wants_action() && !Jack::NoCard.wants_action() && !Jack::Unknown.wants_action());
        assert!(Jack::Reboot(Codec::Ac101).words().contains("restart"));
        assert!(Jack::Switch(Codec::Ac101).words().contains("AC101"));
    }

    #[test]
    fn the_l3_rung_as_dsu_dyn_reports_it() {
        assert_eq!(rung_mhz(Some("1352000000\n".into())), Some(1352));
        assert_eq!(rung_mhz(Some("0\n".into())), None);
        assert_eq!(rung_mhz(None), None); // a module built before the parameter existed
    }
}
