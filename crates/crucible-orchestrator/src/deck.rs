//! Bare-metal Steam Deck execution backend.
//!
//! Mirrors the role `VmManager` + `KernelBuilder` play for the virtme-ng
//! lane, but the "machine" is a physical Steam Deck reached over SSH:
//!
//!   provision/apply -> cross-build the neptune kernel on the workstation
//!                      (native `make`, stripped modules) -> stage artifacts
//!                      to the Deck -> `deck-slot-b.sh install-kernel`
//!                      -> select slot B -> reboot -> poll TCP health
//!                      -> mark the boot good -> (re)start the guest agent.
//!
//! Slot A is the pristine recovery anchor and is never touched. All the
//! SteamOS-specific slot-B mechanics live in `testbed/deck/deck-slot-b.sh`; this
//! module only orchestrates it. The fiddly parts (btrfs ro, /etc overlay,
//! grub regen, boot-attempt fallback) are documented there.
//!
//! Patch apply/revert reuse plain `git` on the Deck kernel tree — identical
//! to the VM lane — so a cycle's patch is always measured against the base.

use anyhow::{Context, Result};
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio::process::Command;
use tokio::time::sleep;

use crate::config::DeckConfig;

/// Marker for what kernel is currently deployed/booted on slot B, so
/// `provision` can skip a redundant base build+boot within a cycle.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Deployed {
    /// Nothing deployed this cycle yet — provision must build+deploy base.
    None,
    /// The base (unpatched) kernel is deployed and booted.
    Base,
    /// A patched kernel is deployed and booted.
    Patched,
}

pub struct DeckBackend {
    cfg: DeckConfig,
    kernel_src: PathBuf,
    /// Release string of the built kernel, e.g.
    /// `6.16.12-valve24.4-1-neptune-616` (from `make kernelrelease`).
    release: Option<String>,
    deployed: Deployed,
}

impl DeckBackend {
    pub fn new(cfg: DeckConfig) -> Self {
        let kernel_src = PathBuf::from(&cfg.kernel_src);
        Self {
            cfg,
            kernel_src,
            release: None,
            deployed: Deployed::None,
        }
    }

    /// Transport fragment threaded into the profiler `TaskEnvelope.context`
    /// so the host-side agent's `GuestRpc` connects over TCP to the Deck.
    pub fn guest_transport(&self) -> serde_json::Value {
        serde_json::json!({
            "deck_host": self.cfg.host,
            "deck_port": self.cfg.agent_port,
        })
    }

    // --- SSH helpers -------------------------------------------------------

    fn ssh_target(&self) -> String {
        format!("{}@{}", self.cfg.user, self.cfg.host)
    }

    /// Base `ssh` argv (key, batch/no-interactive, tolerant host-key policy
    /// — slot B carries A's cloned host keys, but a reflash could change
    /// them and we must never wedge on a prompt).
    fn ssh_args(&self) -> Vec<String> {
        vec![
            "-i".into(),
            self.cfg.ssh_key.clone(),
            "-o".into(),
            "BatchMode=yes".into(),
            "-o".into(),
            "StrictHostKeyChecking=no".into(),
            "-o".into(),
            "UserKnownHostsFile=/dev/null".into(),
            "-o".into(),
            "ConnectTimeout=10".into(),
            self.ssh_target(),
        ]
    }

    /// Run a remote command over SSH, returning stdout on success.
    async fn ssh(&self, remote_cmd: &str) -> Result<String> {
        let mut args = self.ssh_args();
        args.push(remote_cmd.to_string());
        let out = Command::new("ssh")
            .args(&args)
            .output()
            .await
            .with_context(|| format!("ssh {remote_cmd} failed to spawn"))?;
        if !out.status.success() {
            anyhow::bail!(
                "ssh `{remote_cmd}` failed ({}): {}",
                out.status,
                String::from_utf8_lossy(&out.stderr)
            );
        }
        Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
    }

    /// Run the Deck-side control script with a subcommand (via sudo).
    async fn slot_ctl(&self, subcmd: &str) -> Result<String> {
        self.ssh(&format!("sudo {} {}", self.cfg.remote_script, subcmd))
            .await
    }

    // --- Build -------------------------------------------------------------

    /// `make kernelrelease` (cached) — names the modules dir + boot files.
    async fn kernel_release(&mut self) -> Result<String> {
        if let Some(r) = &self.release {
            return Ok(r.clone());
        }
        let out = Command::new("make")
            .args(["LOCALVERSION=", "-s", "kernelrelease"])
            .current_dir(&self.kernel_src)
            .output()
            .await
            .context("make kernelrelease failed")?;
        if !out.status.success() {
            anyhow::bail!(
                "make kernelrelease failed: {}",
                String::from_utf8_lossy(&out.stderr)
            );
        }
        let release = String::from_utf8_lossy(&out.stdout).trim().to_string();
        anyhow::ensure!(!release.is_empty(), "empty kernel release string");
        self.release = Some(release.clone());
        Ok(release)
    }

    /// Wrap a program in `taskset -c <build_cpus>` when a CPU list is set,
    /// keeping the Deck cross-build off the VM lane's cores.
    fn pinned(&self, program: &str, args: &[&str]) -> (String, Vec<String>) {
        if self.cfg.build_cpus.trim().is_empty() {
            (program.to_string(), args.iter().map(|s| s.to_string()).collect())
        } else {
            let mut v = vec![
                "-c".to_string(),
                self.cfg.build_cpus.clone(),
                program.to_string(),
            ];
            v.extend(args.iter().map(|s| s.to_string()));
            ("taskset".to_string(), v)
        }
    }

    /// Native cross-build: `make LOCALVERSION= -jN bzImage modules`, then a
    /// stripped `modules_install` into a per-tree stage dir. Returns
    /// (bzImage path, module-stage dir, release).
    async fn build(&mut self) -> Result<(PathBuf, PathBuf, String)> {
        let release = self.kernel_release().await?;
        let jobs = format!("-j{}", self.cfg.build_jobs);
        let (prog, args) = self.pinned("make", &["LOCALVERSION=", &jobs, "bzImage", "modules"]);
        tracing::info!(release = %release, "cross-building Deck kernel");
        let out = Command::new(&prog)
            .args(&args)
            .current_dir(&self.kernel_src)
            .output()
            .await
            .context("Deck kernel build failed to spawn")?;
        if !out.status.success() {
            anyhow::bail!(
                "Deck kernel build failed: {}",
                String::from_utf8_lossy(&out.stderr).chars().rev().take(2000).collect::<String>().chars().rev().collect::<String>()
            );
        }

        // Stripped modules_install — unstripped neptune modules are ~2G
        // (BTF/debug) and overflow the Deck's 5G slot; stripped is ~175M.
        let stage = self.kernel_src.join(".crucible_deck_modstage");
        let _ = tokio::fs::remove_dir_all(&stage).await;
        tokio::fs::create_dir_all(&stage)
            .await
            .context("create module stage dir")?;
        let install_mod_path = format!("INSTALL_MOD_PATH={}", stage.display());
        let (prog, args) = self.pinned(
            "make",
            &[
                "LOCALVERSION=",
                &jobs,
                &install_mod_path,
                "INSTALL_MOD_STRIP=1",
                "modules_install",
            ],
        );
        let out = Command::new(&prog)
            .args(&args)
            .current_dir(&self.kernel_src)
            .output()
            .await
            .context("modules_install failed to spawn")?;
        if !out.status.success() {
            anyhow::bail!(
                "modules_install failed: {}",
                String::from_utf8_lossy(&out.stderr)
            );
        }

        let bzimage = self.kernel_src.join("arch/x86/boot/bzImage");
        anyhow::ensure!(bzimage.exists(), "bzImage missing after build");
        Ok((bzimage, stage, release))
    }

    // --- Deploy ------------------------------------------------------------

    /// Stage artifacts to the Deck and run `install-kernel`.
    async fn deploy(&self, bzimage: &Path, modstage: &Path, release: &str) -> Result<()> {
        let target = self.ssh_target();
        // Ensure remote layout, write release marker.
        self.ssh(&format!(
            "mkdir -p {dir}/modules && printf '%s' '{rel}' > {dir}/release",
            dir = self.cfg.deploy_dir,
            rel = release
        ))
        .await
        .context("prepare deploy dir")?;

        // Push bzImage.
        let scp_key = self.cfg.ssh_key.clone();
        let bz_dest = format!("{target}:{}/bzImage", self.cfg.deploy_dir);
        let out = Command::new("scp")
            .args([
                "-i",
                &scp_key,
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                &bzimage.to_string_lossy(),
                &bz_dest,
            ])
            .output()
            .await
            .context("scp bzImage failed to spawn")?;
        anyhow::ensure!(
            out.status.success(),
            "scp bzImage failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );

        // Rsync the (small, stripped) module tree, deleting stale files.
        let mod_src = modstage.join("lib/modules").join(release);
        let mod_dest = format!("{target}:{}/modules/", self.cfg.deploy_dir);
        let ssh_e = format!(
            "ssh -i {} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
            self.cfg.ssh_key
        );
        let out = Command::new("rsync")
            .args([
                "-a",
                "--delete",
                "-e",
                &ssh_e,
                &format!("{}", mod_src.to_string_lossy()),
                &mod_dest,
            ])
            .output()
            .await
            .context("rsync modules failed to spawn")?;
        anyhow::ensure!(
            out.status.success(),
            "rsync modules failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );

        // Install into slot B (mkinitcpio + grub regen happen Deck-side).
        self.slot_ctl("install-kernel")
            .await
            .context("install-kernel on Deck")?;
        Ok(())
    }

    // --- Boot --------------------------------------------------------------

    /// Select slot B, reboot, poll TCP health until the guest agent answers,
    /// verify the running kernel is our release, mark the boot good, and
    /// (re)start the guest agent. `release` is the expected `uname -r`.
    async fn boot_slot_b(&self, release: &str) -> Result<()> {
        self.slot_ctl("select-b").await.context("select slot B")?;
        // Capture the pre-reboot boot_id so we can POSITIVELY detect that the
        // Deck actually rebooted. Without this, a slow `systemctl reboot`
        // whose SSH still answers from the pre-reboot state would look like a
        // wrong-kernel fallback and abort a good deploy.
        let old_boot_id = self
            .ssh("cat /proc/sys/kernel/random/boot_id")
            .await
            .unwrap_or_default()
            .trim()
            .to_string();
        // Fire the reboot; the SSH connection drops as the Deck goes down.
        let _ = self.ssh("sudo systemctl reboot").await;
        tracing::info!("Deck rebooting into slot B");

        // Give it a moment to actually go down before polling comes back up.
        sleep(Duration::from_secs(15)).await;

        let deadline = Duration::from_secs(self.cfg.boot_timeout_secs.max(120));
        let started = tokio::time::Instant::now();
        loop {
            if started.elapsed() > deadline {
                anyhow::bail!(
                    "Deck did not come back on slot B within {}s (fallback to A may have occurred)",
                    deadline.as_secs()
                );
            }
            if let Ok(out) = self.ssh("cat /proc/sys/kernel/random/boot_id; uname -r").await {
                let mut lines = out.lines();
                let boot_id = lines.next().unwrap_or("").trim();
                let uname = lines.next().unwrap_or("").trim();
                // Still the pre-reboot boot session (SSH answered before the
                // box went down) — keep waiting, do NOT judge the kernel yet.
                if !old_boot_id.is_empty() && boot_id == old_boot_id {
                    sleep(Duration::from_secs(5)).await;
                    continue;
                }
                // Fresh boot (new boot_id, or we never captured the old one).
                if uname == release {
                    tracing::info!(kernel = %uname, "Deck booted slot B on test kernel");
                    break;
                }
                // Reachable on a fresh boot with the wrong kernel = chainloader
                // fell back to A.
                anyhow::bail!(
                    "Deck came back on kernel `{}`, expected `{}` (fell back to slot A)",
                    uname,
                    release
                );
            }
            sleep(Duration::from_secs(5)).await;
        }

        // Neutralize the unknown chainloader retry budget: explicitly mark
        // this boot good so a healthy B is never falsely reverted.
        self.slot_ctl("mark-good").await.context("mark boot good")?;
        // Start the guest agent (TCP) with the perfetto env.
        self.slot_ctl("start-agent")
            .await
            .context("start guest agent on Deck")?;
        self.wait_agent().await?;
        Ok(())
    }

    /// Poll the guest agent's TCP health_check until it answers.
    async fn wait_agent(&self) -> Result<()> {
        let deadline = Duration::from_secs(60);
        let started = tokio::time::Instant::now();
        loop {
            if started.elapsed() > deadline {
                anyhow::bail!("guest agent did not become reachable on TCP {}", self.cfg.agent_port);
            }
            if self.agent_health().await.is_ok() {
                return Ok(());
            }
            sleep(Duration::from_secs(3)).await;
        }
    }

    /// One length-prefixed JSON `health_check` over TCP.
    async fn agent_health(&self) -> Result<()> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let addr = format!("{}:{}", self.cfg.host, self.cfg.agent_port);
        let mut stream = tokio::net::TcpStream::connect(&addr)
            .await
            .with_context(|| format!("connect {addr}"))?;
        let body = br#"{"cmd":"health_check"}"#;
        let mut framed = (body.len() as u32).to_be_bytes().to_vec();
        framed.extend_from_slice(body);
        stream.write_all(&framed).await?;
        let mut len_buf = [0u8; 4];
        stream.read_exact(&mut len_buf).await?;
        let len = u32::from_be_bytes(len_buf) as usize;
        anyhow::ensure!(len > 0 && len < 65536, "bad health_check frame len {len}");
        let mut resp = vec![0u8; len];
        stream.read_exact(&mut resp).await?;
        anyhow::ensure!(
            String::from_utf8_lossy(&resp).contains("\"ok\""),
            "health_check not ok: {}",
            String::from_utf8_lossy(&resp)
        );
        Ok(())
    }

    // --- Backend surface (called from orchestrator) ------------------------

    /// Build (base, if not already) + deploy + boot slot B. Idempotent
    /// within a cycle: if the base kernel is already booted, no-op.
    pub async fn provision(&mut self) -> Result<()> {
        if self.deployed == Deployed::Base {
            tracing::info!("Deck already on base kernel, skipping provision");
            return Ok(());
        }
        let (bzimage, modstage, release) = self.build().await?;
        self.deploy(&bzimage, &modstage, &release).await?;
        self.boot_slot_b(&release).await?;
        self.deployed = Deployed::Base;
        Ok(())
    }

    /// Reboot slot B on the currently-installed kernel (phase boundary).
    pub async fn reboot_same_kernel(&mut self) -> Result<()> {
        let release = self.kernel_release().await?;
        self.boot_slot_b(&release).await
    }

    /// git-apply a patch, cross-build, deploy, boot. On build failure the
    /// patch is reverted and the error propagates (cycle continues).
    pub async fn apply_changes(&mut self, patch_path: &str) -> Result<()> {
        self.git_apply(patch_path).await?;
        let built = self.build().await;
        let (bzimage, modstage, release) = match built {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!(err = %e, "Deck build failed, reverting patch");
                let _ = self.revert_patch().await;
                return Err(e);
            }
        };
        self.deploy(&bzimage, &modstage, &release).await?;
        self.boot_slot_b(&release).await?;
        self.deployed = Deployed::Patched;
        Ok(())
    }

    async fn git_apply(&self, patch_path: &str) -> Result<()> {
        let out = Command::new("git")
            .args([
                "-C",
                &self.kernel_src.to_string_lossy(),
                "apply",
                patch_path,
            ])
            .output()
            .await
            .context("git apply failed to spawn")?;
        anyhow::ensure!(
            out.status.success(),
            "git apply failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        Ok(())
    }

    /// Revert the working tree to the committed base (drops the applied
    /// patch; the committed -Werror/base fixes survive).
    pub async fn revert_patch(&self) -> Result<()> {
        let out = Command::new("git")
            .args([
                "-C",
                &self.kernel_src.to_string_lossy(),
                "checkout",
                "--",
                ".",
            ])
            .output()
            .await
            .context("git checkout failed to spawn")?;
        anyhow::ensure!(
            out.status.success(),
            "git checkout failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        Ok(())
    }

    /// No teardown for a physical machine — the Deck simply stays on slot B
    /// between cycles. (Kept for surface parity with `VmManager::shutdown`.)
    pub async fn shutdown(&mut self) -> Result<()> {
        Ok(())
    }

    /// Reset the per-cycle deploy marker so the next cycle rebuilds/boots the
    /// base kernel (mirrors clearing `current_kernel` on the VM lane).
    pub fn reset_cache(&mut self) {
        self.deployed = Deployed::None;
    }

    /// Best-effort sysctl application on the running slot-B kernel via SSH.
    pub async fn apply_sysctls(
        &self,
        sysctls: serde_json::Map<String, serde_json::Value>,
    ) -> Result<serde_json::Value> {
        let mut results = serde_json::Map::new();
        for (k, v) in sysctls {
            let val = v
                .as_str()
                .map(|s| s.to_string())
                .unwrap_or_else(|| v.to_string());
            // SECURITY: keys/values come from the Optimizer's model output and
            // are interpolated into a command run as root over SSH. Reject
            // anything that isn't a plain sysctl key/value so a metacharacter
            // (`;`, `` ` ``, `$()`, `|`) can't execute arbitrary commands on
            // the physical Deck. Mirrors the guest agent's _SYSCTL_KEY_RE guard.
            if !is_safe_sysctl_key(&k) || !is_safe_sysctl_value(&val) {
                results.insert(
                    k,
                    serde_json::json!({"ok": false, "err": "rejected: unsafe sysctl key/value"}),
                );
                continue;
            }
            // Value may be a space-separated numeric list; quote it (safe —
            // validation above rejects `"`, `$`, backtick and other metachars).
            let res = self.ssh(&format!("sudo sysctl -w \"{k}={val}\"")).await;
            results.insert(
                k,
                match res {
                    Ok(o) => serde_json::json!({"ok": true, "out": o}),
                    Err(e) => serde_json::json!({"ok": false, "err": e.to_string()}),
                },
            );
        }
        Ok(serde_json::Value::Object(results))
    }
}

/// A sysctl key is a dotted/slashed name: `kernel.sched_latency_ns` or a
/// `/proc/sys/...` path. Allow only chars that appear in such names.
fn is_safe_sysctl_key(k: &str) -> bool {
    !k.is_empty()
        && k.len() <= 256
        && k.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-' | '/'))
}

/// sysctl values are numbers or comma/space-separated numeric lists (e.g.
/// `1`, `0 0 0`, `4096,8192`). Disallow shell metacharacters entirely.
fn is_safe_sysctl_value(v: &str) -> bool {
    !v.is_empty()
        && v.len() <= 256
        && v.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-' | ',' | ' '))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> DeckConfig {
        DeckConfig {
            host: "192.168.86.80".into(),
            user: "deck".into(),
            ssh_key: "/home/void/.ssh/crucible_deck_ed25519".into(),
            kernel_src: "/home/void/upstream/crucible_kernel_2".into(),
            build_cpus: "0-7,16-23".into(),
            build_jobs: 16,
            remote_script: "/home/deck/deck-slot-b.sh".into(),
            deploy_dir: "/home/deck/deck-deploy".into(),
            boot_timeout_secs: 60,
            agent_port: 5000,
            kernel_name: "linux-neptune-616".into(),
            agent_dir: "/home/deck/crucible-agent".into(),
        }
    }

    #[test]
    fn transport_is_tcp() {
        let b = DeckBackend::new(cfg());
        let t = b.guest_transport();
        assert_eq!(t["deck_host"], "192.168.86.80");
        assert_eq!(t["deck_port"], 5000);
    }

    #[test]
    fn ssh_target_format() {
        let b = DeckBackend::new(cfg());
        assert_eq!(b.ssh_target(), "deck@192.168.86.80");
    }

    #[test]
    fn pinned_wraps_taskset() {
        let b = DeckBackend::new(cfg());
        let (prog, args) = b.pinned("make", &["bzImage"]);
        assert_eq!(prog, "taskset");
        assert_eq!(args[0], "-c");
        assert_eq!(args[1], "0-7,16-23");
        assert_eq!(args[2], "make");
        assert_eq!(args[3], "bzImage");
    }

    #[test]
    fn pinned_noop_without_cpus() {
        let mut c = cfg();
        c.build_cpus = String::new();
        let b = DeckBackend::new(c);
        let (prog, args) = b.pinned("make", &["bzImage"]);
        assert_eq!(prog, "make");
        assert_eq!(args, vec!["bzImage".to_string()]);
    }

    #[test]
    fn deploy_marker_starts_none() {
        let b = DeckBackend::new(cfg());
        assert_eq!(b.deployed, Deployed::None);
    }

    #[test]
    fn sysctl_key_validation_rejects_injection() {
        assert!(is_safe_sysctl_key("kernel.sched_latency_ns"));
        assert!(is_safe_sysctl_key("vm.max_map_count"));
        assert!(is_safe_sysctl_key("/proc/sys/kernel/foo"));
        // injection attempts
        assert!(!is_safe_sysctl_key("kernel.x; rm -rf /"));
        assert!(!is_safe_sysctl_key("$(reboot)"));
        assert!(!is_safe_sysctl_key("a`id`"));
        assert!(!is_safe_sysctl_key("a|b"));
        assert!(!is_safe_sysctl_key(""));
    }

    #[test]
    fn sysctl_value_validation() {
        assert!(is_safe_sysctl_value("1"));
        assert!(is_safe_sysctl_value("0 0 0"));
        assert!(is_safe_sysctl_value("4096,8192,16384"));
        assert!(!is_safe_sysctl_value("1; reboot"));
        assert!(!is_safe_sysctl_value("$(cat /etc/shadow)"));
        assert!(!is_safe_sysctl_value(""));
    }
}
