use anyhow::{Context, Result};
use std::time::Duration;
use tokio::process::{Child, Command};

use crate::config::VmConfig;

/// Parse a CPU list like "8-15" or "8,9,10,11" (mixed forms allowed) into
/// an ordered list of host CPU ids. vCPU N pins to the Nth entry.
pub fn parse_cpu_list(spec: &str) -> Result<Vec<usize>> {
    let mut cpus = Vec::new();
    for part in spec.split(',') {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        if let Some((lo, hi)) = part.split_once('-') {
            let lo: usize = lo.trim().parse().with_context(|| format!("bad cpu {lo}"))?;
            let hi: usize = hi.trim().parse().with_context(|| format!("bad cpu {hi}"))?;
            if hi < lo {
                anyhow::bail!("inverted cpu range {part}");
            }
            cpus.extend(lo..=hi);
        } else {
            cpus.push(part.parse().with_context(|| format!("bad cpu {part}"))?);
        }
    }
    if cpus.is_empty() {
        anyhow::bail!("empty cpu list: {spec:?}");
    }
    Ok(cpus)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VmState {
    Stopped,
    Booting,
    Running,
    Failed,
}

pub struct VmManager {
    config: VmConfig,
    state: VmState,
    child: Option<Child>,
}

impl VmManager {
    pub fn new(config: VmConfig) -> Self {
        Self {
            config,
            state: VmState::Stopped,
            child: None,
        }
    }

    pub fn state(&self) -> VmState {
        self.state
    }

    /// Where the vng/QEMU console is teed. Next to the kernel source so it is
    /// easy to find; a stable name (append mode preserves reboot history).
    fn boot_log_path(&self) -> std::path::PathBuf {
        std::path::Path::new(&self.config.kernel_src)
            .parent()
            .unwrap_or_else(|| std::path::Path::new("/tmp"))
            .join("crucible-vm-boot.log")
    }

    pub fn build_boot_command(
        &self,
        _kernel_path: &str,
        vfio_functions: &[String],
        module_overlay: Option<&str>,
    ) -> Vec<String> {
        // virtme-ng 1.35 picks up `arch/x86/boot/bzImage` from the cwd —
        // there is no `--kernel` flag and no `--boot` subcommand. The
        // caller must spawn this with current_dir set to the kernel src.
        //
        // --qemu-opts must use the `=` form because argparse otherwise
        // mis-reads any value starting with `-` as a new option.
        //
        // --exec replaces virtme-init's interactive shell with the guest
        // agent so the VM exits as soon as the agent does (and lives as
        // long as we keep it alive). The synthetic loop doesn't need
        // systemd to start; the agent itself is the only service.
        let mut qemu_opts = String::new();
        let configured = self.config.vfio_device.trim();
        for func in vfio_functions {
            if func == configured {
                // rombar=0 on the configured (VGA) function: QEMU's
                // option-ROM read of a GPU the host previously drove hangs
                // device realization forever (observed on a live-unbound
                // 7900 XT). Keyed on the config value, not list position —
                // sibling discovery order is not a contract.
                qemu_opts.push_str(&format!("-device vfio-pci,host={func},rombar=0 "));
            } else {
                qemu_opts.push_str(&format!("-device vfio-pci,host={func} "));
            }
        }
        qemu_opts.push_str(&format!(
            "-device vhost-vsock-pci,guest-cid={} ",
            self.config.vsock_cid
        ));
        // Slirp userspace networking: the Steam client's CM logon needs a
        // route out (the guest agent DHCPs the interface itself). Harmless
        // for the synthetic loop — the NIC just sits idle.
        qemu_opts.push_str("-netdev user,id=net0 -device virtio-net-pci,netdev=net0");
        let topology = self.config.smp_topology.trim();
        if !topology.is_empty() {
            // Appended after vng's own `-smp <cpus>`; QEMU takes the last
            // -smp, so this override gives the guest the real core/thread
            // structure (vng exposes no topology flags itself).
            qemu_opts.push_str(&format!(" -smp {},{}", self.config.cpus, topology));
        }
        if !self.config.vcpu_pin_cpus.trim().is_empty() {
            // QEMU names its vCPU threads ("CPU N/KVM") ONLY under
            // debug-threads=on — without it every thread reads
            // "qemu-system-x86" and pin_vcpus finds nothing. Overrides
            // vng's own -name (last one wins).
            qemu_opts.push_str(" -name crucible-vm,debug-threads=on");
        }
        let guest_cmd =
            "cd /opt/crucible && PYTHONPATH=/opt/crucible \
             exec python3 -m guest.crucible_guest_agent"
                .to_string();
        let mut args = vec![
            "vng".to_string(),
            "--memory".to_string(),
            self.config.memory.clone(),
            "--cpus".to_string(),
            self.config.cpus.to_string(),
            "--root".to_string(),
            self.config.guest_rootfs.clone(),
        ];
        let payload = self.config.guest_payload.trim();
        if !payload.is_empty() {
            // virtme-ng exposes the host dir read-only inside the guest at
            // /opt/crucible/guest. Overlays whatever the rootfs has there,
            // so updating the agent doesn't require a rootfs rebuild.
            args.push("--rodir".to_string());
            args.push(format!("/opt/crucible/guest={}", payload));
        }
        if let Some(overlay) = module_overlay {
            // With --root, vng resolves modules from <root>/lib/modules and
            // ignores the build tree's .virtme_mods — overlay it so the
            // guest can modprobe drivers (amdgpu) built with the kernel.
            args.push("--rodir".to_string());
            args.push(overlay.to_string());
        }
        args.push("--exec".to_string());
        args.push(guest_cmd);
        args.push(format!("--qemu-opts={}", qemu_opts));
        args
    }

    /// All PCI functions of the configured VFIO device's slot, short form,
    /// sorted ("03:00.0" → ["03:00.0", "03:00.1", ...]). Multifunction GPUs
    /// (VGA + HDMI audio + USB + UCSI) reset as a unit: QEMU refuses the
    /// attach unless every function is owned by vfio. Returns empty when
    /// passthrough is disabled.
    pub fn vfio_sibling_functions(&self) -> Vec<String> {
        let dev = self.config.vfio_device.trim();
        if dev.is_empty() || dev.eq_ignore_ascii_case("none") {
            return Vec::new();
        }
        Self::vfio_sibling_functions_with(dev, |prefix| {
            std::fs::read_dir("/sys/bus/pci/devices")
                .map(|entries| {
                    entries
                        .filter_map(|e| e.ok())
                        .map(|e| e.file_name().to_string_lossy().into_owned())
                        .filter(|name| name.starts_with(prefix))
                        .collect()
                })
                .unwrap_or_default()
        })
    }

    /// Testable core of `vfio_sibling_functions`: `list_matching` returns
    /// the domain-qualified device names under /sys/bus/pci/devices that
    /// start with the given prefix.
    fn vfio_sibling_functions_with(
        dev: &str,
        list_matching: impl Fn(&str) -> Vec<String>,
    ) -> Vec<String> {
        let qualified = if dev.matches(':').count() == 1 {
            format!("0000:{dev}")
        } else {
            dev.to_string()
        };
        // "0000:03:00.0" → slot prefix "0000:03:00."
        let prefix = match qualified.rfind('.') {
            Some(pos) => &qualified[..=pos],
            None => return vec![dev.to_string()],
        };
        let mut funcs: Vec<String> = list_matching(prefix)
            .into_iter()
            .map(|name| {
                name.strip_prefix("0000:")
                    .map(str::to_string)
                    .unwrap_or(name)
            })
            .collect();
        funcs.sort();
        if funcs.is_empty() {
            // Sysfs unavailable (tests, containers): fall back to the
            // configured function so passthrough still gets attempted.
            return vec![dev.to_string()];
        }
        funcs
    }

    /// vng `--rodir` mapping for the kernel build's modules, or None when
    /// no installed module tree exists. The build step is expected to have
    /// run `make modules_install INSTALL_MOD_PATH=.virtme_mods`.
    ///
    /// The module dir must match the *running* kernel's release exactly
    /// (modprobe looks in /lib/modules/$(uname -r)). Stale trees from
    /// earlier configs accumulate in .virtme_mods — e.g. "7.1.0-rc7+"
    /// next to "7.1.0-rc7-virtme" after `vng --build` changed the
    /// localversion — so the release is read from the build tree's
    /// include/config/kernel.release, never guessed from readdir order.
    pub fn find_module_overlay(kernel_src: &str) -> Option<String> {
        let src = std::path::Path::new(kernel_src);
        let kver = std::fs::read_to_string(src.join("include/config/kernel.release"))
            .ok()?
            .trim()
            .to_string();
        if kver.is_empty() {
            return None;
        }
        let mods_dir = src.join(".virtme_mods").join("lib/modules").join(&kver);
        if !mods_dir.is_dir() {
            return None;
        }
        Some(format!("/lib/modules/{kver}={}", mods_dir.display()))
    }

    /// Kernel-source path the vng invocation must be run from.
    pub fn kernel_src(&self) -> &str {
        &self.config.kernel_src
    }

    /// Fail fast if the configured VFIO device — or any sibling function of
    /// its slot — is not actually bound to vfio-pci. A GPU still bound to
    /// amdgpu hangs the QEMU boot, and an unbound sibling makes QEMU fail
    /// the bus reset with "depends on group N which is not owned".
    /// No-op when passthrough is disabled (`vfio_device` empty or "none").
    pub fn validate_passthrough(&self) -> Result<()> {
        let dev = self.config.vfio_device.trim();
        if dev.is_empty() || dev.eq_ignore_ascii_case("none") {
            return Ok(());
        }
        Self::validate_passthrough_functions_with(&self.vfio_sibling_functions(), |addr| {
            std::fs::read_link(format!("/sys/bus/pci/devices/{addr}/driver"))
                .ok()
                .and_then(|p| p.file_name().map(|s| s.to_string_lossy().into_owned()))
        })
    }

    /// Validate every function of the slot against the injected sysfs
    /// reader; first offender fails the whole check.
    fn validate_passthrough_functions_with(
        functions: &[String],
        read_driver: impl Fn(&str) -> Option<String>,
    ) -> Result<()> {
        for func in functions {
            Self::validate_passthrough_with(func, &read_driver)?;
        }
        Ok(())
    }

    /// Testable core of `validate_passthrough`: `read_driver` maps a full
    /// PCI address (e.g. "0000:0a:00.0") to the bound driver name, or None
    /// when no driver is bound / the device doesn't exist.
    fn validate_passthrough_with(
        vfio_device: &str,
        read_driver: impl Fn(&str) -> Option<String>,
    ) -> Result<()> {
        let dev = vfio_device.trim();
        if dev.is_empty() || dev.eq_ignore_ascii_case("none") {
            return Ok(());
        }
        // Config uses the short "0a:00.0" form (matching qemu's vfio-pci
        // host=); sysfs wants the domain-qualified form.
        let addr = if dev.matches(':').count() == 1 {
            format!("0000:{dev}")
        } else {
            dev.to_string()
        };
        match read_driver(&addr) {
            Some(driver) if driver == "vfio-pci" => Ok(()),
            Some(driver) => anyhow::bail!(
                "vfio device {addr} is bound to '{driver}', not vfio-pci; \
                 run scripts/setup-host.sh to bind it before booting"
            ),
            None => anyhow::bail!(
                "vfio device {addr} has no driver bound (or does not exist); \
                 run scripts/setup-host.sh and check the PCI address"
            ),
        }
    }

    pub async fn boot(&mut self, kernel_path: &str) -> Result<()> {
        if self.state != VmState::Stopped {
            anyhow::bail!("VM is not stopped (current state: {:?})", self.state);
        }
        self.state = VmState::Booting;

        let vfio_functions = self.vfio_sibling_functions();
        let module_overlay = Self::find_module_overlay(&self.config.kernel_src);
        let cmd_args =
            self.build_boot_command(kernel_path, &vfio_functions, module_overlay.as_deref());
        tracing::info!(kernel = kernel_path, cmd = %cmd_args.join(" "), "booting VM");

        // Capture the vng/QEMU console to a file. Previously stdout/stderr
        // were piped and never read — an unread pipe can wedge the child when
        // the buffer fills, and worse, a boot failure (QEMU vfio error, guest
        // amdgpu hang, kernel panic) left no diagnostic at all: the only
        // symptom was a downstream vsock "No such device" from wait_for_ready.
        // Append mode keeps every boot in one file so a reboot's console is
        // not clobbered by the next attempt.
        let boot_log_path = self.boot_log_path();
        {
            use std::io::Write;
            if let Ok(mut f) = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&boot_log_path)
            {
                let _ = writeln!(
                    f,
                    "\n===== crucible boot: kernel={kernel_path} =====",
                );
            }
        }
        let boot_log = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&boot_log_path)
            .with_context(|| format!("failed to open boot log {boot_log_path:?}"))?;
        let boot_log_err = boot_log
            .try_clone()
            .context("failed to clone boot-log handle for stderr")?;

        let mut command = Command::new(&cmd_args[0]);
        command
            .args(&cmd_args[1..])
            .current_dir(&self.config.kernel_src)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::from(boot_log))
            .stderr(std::process::Stdio::from(boot_log_err))
            // If the orchestrator crashes or the test panics, send SIGKILL
            // to vng/QEMU so it doesn't keep CID 3 reserved on the host.
            // kill_on_drop alone is not enough: vng wraps virtme-run in a
            // `sh -c` chain and the QEMU grandchild survives the parent's
            // SIGKILL (observed leak after a failed e2e). A dedicated
            // process group lets shutdown() kill the whole tree.
            .process_group(0)
            .kill_on_drop(true);
        let child = command.spawn().with_context(|| {
            format!("failed to spawn vng: {}", cmd_args.join(" "))
        })?;

        self.child = Some(child);
        self.state = VmState::Running;
        Ok(())
    }

    pub async fn wait_for_ready(
        &self,
        vsock_client: &crate::vsock_client::VsockClient,
        timeout: Duration,
    ) -> Result<()> {
        let start = std::time::Instant::now();
        let poll_interval = Duration::from_secs(2);
        loop {
            if start.elapsed() > timeout {
                anyhow::bail!(
                    "VM failed to become ready within {}s",
                    timeout.as_secs()
                );
            }
            if let Ok(crucible_common::protocol::GuestResponse::Ok { .. }) =
                vsock_client.health_check().await
            {
                tracing::info!("VM is ready");
                // vCPU threads exist once the guest is up; pin them now so
                // every measurement runs with the same CPU placement.
                // Non-fatal: an unpinned run is a noisier run, not a broken
                // one.
                if !self.config.vcpu_pin_cpus.is_empty() {
                    match self.pin_vcpus() {
                        Ok(n) => tracing::info!(
                            pinned = n,
                            cpus = %self.config.vcpu_pin_cpus,
                            "vCPU threads pinned"
                        ),
                        Err(e) => {
                            tracing::warn!(error = %e, "vCPU pinning failed; continuing unpinned")
                        }
                    }
                }
                return Ok(());
            }
            tokio::time::sleep(poll_interval).await;
        }
    }

    /// Pin each `CPU N/KVM` thread of our QEMU grandchild 1:1 onto the
    /// configured host CPU list. Returns the number of threads pinned.
    fn pin_vcpus(&self) -> Result<usize> {
        let cpus = parse_cpu_list(&self.config.vcpu_pin_cpus)?;
        let emulator_cpus = {
            let spec = self.config.emulator_pin_cpus.trim();
            if spec.is_empty() {
                None
            } else {
                Some(parse_cpu_list(spec)?)
            }
        };
        let pgid = self
            .child
            .as_ref()
            .and_then(|c| c.id())
            .context("no VM child process")? as i32;

        // vng wraps QEMU in a sh -c chain, so find the grandchild by
        // process group: we created the group via process_group(0), so its
        // pgid equals the direct child's pid.
        let mut pinned = 0usize;
        let mut qemu_pids: Vec<i32> = Vec::new();
        for entry in std::fs::read_dir("/proc").context("read /proc")? {
            let Ok(entry) = entry else { continue };
            let Some(pid) = entry
                .file_name()
                .to_str()
                .and_then(|s| s.parse::<i32>().ok())
            else {
                continue;
            };
            let stat = match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
                Ok(s) => s,
                Err(_) => continue,
            };
            // stat: pid (comm) state ppid pgrp ... — comm may contain
            // spaces, so parse from the closing paren.
            let Some(rest) = stat.rsplit_once(')').map(|(_, r)| r) else {
                continue;
            };
            let fields: Vec<&str> = rest.split_whitespace().collect();
            let proc_pgid: i32 = match fields.get(2).and_then(|s| s.parse().ok()) {
                Some(v) => v,
                None => continue,
            };
            if proc_pgid != pgid || !stat.contains("(qemu-system-x86") {
                continue;
            }
            qemu_pids.push(pid);
            let task_dir = format!("/proc/{pid}/task");
            for task in std::fs::read_dir(&task_dir)
                .with_context(|| format!("read {task_dir}"))?
                .flatten()
            {
                let Some(tid) = task
                    .file_name()
                    .to_str()
                    .and_then(|s| s.parse::<i32>().ok())
                else {
                    continue;
                };
                let comm = std::fs::read_to_string(task.path().join("comm"))
                    .unwrap_or_default();
                let comm = comm.trim();
                let Some(idx) = comm
                    .strip_prefix("CPU ")
                    .and_then(|r| r.split('/').next())
                    .and_then(|n| n.parse::<usize>().ok())
                else {
                    // Not a vCPU: an emulator helper (9p workers, main
                    // loop, RCU). Keep it off the vCPU cores when an
                    // emulator set is configured.
                    if let Some(ecpus) = &emulator_cpus {
                        Self::set_affinity(tid, ecpus)?;
                    }
                    continue;
                };
                let Some(&target) = cpus.get(idx) else {
                    anyhow::bail!(
                        "vCPU {idx} has no pin target (vcpu_pin_cpus lists {} CPUs)",
                        cpus.len()
                    );
                };
                Self::set_affinity(tid, &[target])?;
                pinned += 1;
            }
        }
        if pinned == 0 {
            anyhow::bail!("no CPU N/KVM threads found in process group {pgid}");
        }
        // vhost kernel threads (vsock) deliberately stay UNPINNED: vsock
        // is quiet during measurement windows (RPCs/fetches happen between
        // runs), so pinning buys no fidelity, and floating lets the
        // scheduler place vhost near the waiting vCPU (vring locality).
        let _ = &qemu_pids;
        Ok(pinned)
    }

    /// sched_setaffinity a task onto the given CPU set.
    fn set_affinity(tid: i32, cpus: &[usize]) -> Result<()> {
        // SAFETY: plain sched_setaffinity on tids we just enumerated; a
        // stale tid returns ESRCH which we surface.
        unsafe {
            let mut set: libc::cpu_set_t = std::mem::zeroed();
            for &c in cpus {
                libc::CPU_SET(c, &mut set);
            }
            if libc::sched_setaffinity(tid, std::mem::size_of::<libc::cpu_set_t>(), &set)
                != 0
            {
                anyhow::bail!(
                    "sched_setaffinity(tid={tid}, cpus={cpus:?}): {}",
                    std::io::Error::last_os_error()
                );
            }
        }
        Ok(())
    }

    pub async fn shutdown(&mut self) -> Result<()> {
        if let Some(ref mut child) = self.child {
            tracing::info!("shutting down VM");
            // Kill the whole process group (vng → sh -c → virtme-run →
            // qemu); killing only the direct child leaves QEMU running and
            // holding the vsock CID.
            let pgid = child.id().map(|pid| {
                // pid is u32; clamp before negating so the cast can never
                // overflow to i32::MIN (whose meaning kill() reserves).
                -(pid.min(i32::MAX as u32) as i32)
            });
            if let Some(pgid) = pgid {
                // SAFETY: plain libc kill on a pgid we created via
                // process_group(0); no memory at stake.
                let ret = unsafe { libc::kill(pgid, libc::SIGKILL) };
                if ret != 0 {
                    tracing::warn!(
                        pgid,
                        err = %std::io::Error::last_os_error(),
                        "process-group kill failed"
                    );
                }
            }
            // The group SIGKILL may already have reaped the direct child;
            // a kill() error here must not mask a successful shutdown.
            if let Err(err) = child.kill().await {
                tracing::warn!(%err, "direct child kill failed (group already dead?)");
            }
            child
                .wait()
                .await
                .context("failed to wait for VM process")?;

            // child.wait() reaps only the direct vng wrapper. The QEMU
            // grandchild vng spawned lives in the same process group and
            // dies asynchronously after the group SIGKILL — and until it
            // does, it holds the passthrough GPU's /dev/vfio/<group> open.
            // Booting the next VM before that release fails hard with
            //   vfio: Could not open '/dev/vfio/N': Device or resource busy
            // which is exactly how a kernel-patch reboot lost the GPU. Wait
            // for the whole group to drain (kill(-pgid, 0) → ESRCH) before
            // returning, so the vfio group fd is free for the next boot.
            if let Some(pgid) = pgid {
                Self::wait_for_process_group_exit(pgid, Duration::from_secs(15)).await;
                // fds close on process death before reap; a short settle
                // covers any residual vfio-group teardown in the kernel.
                tokio::time::sleep(Duration::from_millis(300)).await;
            }
        }
        self.child = None;
        self.state = VmState::Stopped;
        Ok(())
    }

    /// Poll until no process remains in `pgid`'s group (or `timeout`).
    /// `kill(pgid, 0)` returns 0 while any group member is alive and fails
    /// with ESRCH once the group is empty.
    async fn wait_for_process_group_exit(pgid: i32, timeout: Duration) {
        let deadline = std::time::Instant::now() + timeout;
        loop {
            // SAFETY: signal 0 is a permission/existence probe, sends nothing.
            let alive = unsafe { libc::kill(pgid, 0) } == 0;
            if !alive {
                return;
            }
            if std::time::Instant::now() >= deadline {
                tracing::warn!(
                    pgid,
                    "VM process group still alive after drain timeout; \
                     next boot may hit a busy vfio device"
                );
                return;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn boot_command_appends_smp_topology_override() {
        let toml_str = r#"
            kernel_src = "/tmp/k"
            guest_rootfs = "/tmp/r"
            vfio_device = "none"
            cpus = 16
            smp_topology = "sockets=1,cores=8,threads=2"
            vcpu_pin_cpus = "8,24"
        "#;
        let config: crate::config::VmConfig = toml::from_str(toml_str).unwrap();
        let vm = VmManager::new(config);
        let args = vm.build_boot_command("bzImage", &[], None);
        let qemu_opts = args.iter().find(|a| a.starts_with("--qemu-opts=")).unwrap();
        assert!(
            qemu_opts.contains("-smp 16,sockets=1,cores=8,threads=2"),
            "{qemu_opts}"
        );
        // Pinning requires named vCPU threads (QEMU only names them
        // under debug-threads=on).
        assert!(
            qemu_opts.contains("-name crucible-vm,debug-threads=on"),
            "{qemu_opts}"
        );


        // Empty topology: no -smp override in qemu-opts.
        let toml_str = r#"
            kernel_src = "/tmp/k"
            guest_rootfs = "/tmp/r"
            vfio_device = "none"
        "#;
        let config: crate::config::VmConfig = toml::from_str(toml_str).unwrap();
        let vm = VmManager::new(config);
        let args = vm.build_boot_command("bzImage", &[], None);
        let qemu_opts = args.iter().find(|a| a.starts_with("--qemu-opts=")).unwrap();
        assert!(!qemu_opts.contains("-smp"), "{qemu_opts}");
    }

    #[test]
    fn parse_cpu_list_ranges_and_singles() {
        assert_eq!(super::parse_cpu_list("8-11").unwrap(), vec![8, 9, 10, 11]);
        assert_eq!(super::parse_cpu_list("8,10,12").unwrap(), vec![8, 10, 12]);
        assert_eq!(super::parse_cpu_list("0-1,4").unwrap(), vec![0, 1, 4]);
        assert!(super::parse_cpu_list("").is_err());
        assert!(super::parse_cpu_list("5-2").is_err());
        assert!(super::parse_cpu_list("x").is_err());
    }

    use super::*;

    fn test_vm_config() -> crate::config::VmConfig {
        let toml_str = r#"
            kernel_src = "/home/void/upstream/linux"
            guest_rootfs = "/home/void/.crucible/rootfs"
            memory = "16G"
            cpus = 8
            vfio_device = "03:00.0"
            boot_timeout_secs = 60
            vsock_cid = 3
        "#;
        toml::from_str(toml_str).unwrap()
    }

    fn gpu_functions() -> Vec<String> {
        vec!["03:00.0".to_string()]
    }

    #[test]
    fn build_vng_boot_command() {
        let config = test_vm_config();
        let manager = VmManager::new(config);
        let cmd = manager.build_boot_command("/ignored", &gpu_functions(), None);
        // No more `--boot` or `--kernel` — vng 1.35 picks up bzImage from cwd.
        assert!(!cmd.iter().any(|a| a == "--boot"));
        assert!(!cmd.iter().any(|a| a == "--kernel"));
        assert!(cmd.iter().any(|a| a == "--root"));
        assert!(cmd.iter().any(|a| a == "--memory"));
        assert!(cmd.iter().any(|a| a == "--cpus"));
        assert!(cmd.iter().any(|a| a == "--exec"));
    }

    #[test]
    fn build_vng_boot_command_contains_qemu_opts() {
        let config = test_vm_config();
        let manager = VmManager::new(config);
        let cmd = manager.build_boot_command("/ignored", &gpu_functions(), None);
        let joined = cmd.join(" ");
        // The GPU function carries rombar=0: reading the option ROM of a
        // GPU previously driven by the host hangs QEMU device realization
        // indefinitely (observed on a live-unbound 7900 XT).
        assert!(
            joined.contains("vfio-pci,host=03:00.0,rombar=0"),
            "joined: {}",
            joined
        );
        assert!(joined.contains("vhost-vsock-pci,guest-cid=3"));
        // Slirp NIC for Steam CM logon (guest agent runs dhclient).
        assert!(joined.contains("-netdev user,id=net0"));
        assert!(joined.contains("virtio-net-pci,netdev=net0"));
        // --qemu-opts must use the `=` form so argparse accepts a value
        // that begins with `-`.
        assert!(joined.contains("--qemu-opts=-device "));
    }

    #[test]
    fn build_vng_boot_command_passes_all_gpu_functions() {
        // A 7900 XT is a 4-function device (VGA/audio/USB/UCSI). The bus
        // reset QEMU performs at attach affects every function, so VFIO
        // requires owning all of them — passing only the VGA function makes
        // QEMU fail with "depends on group N which is not owned".
        let config = test_vm_config();
        let manager = VmManager::new(config);
        let funcs: Vec<String> = ["03:00.0", "03:00.1", "03:00.2", "03:00.3"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let cmd = manager.build_boot_command("/ignored", &funcs, None);
        let joined = cmd.join(" ");
        assert!(joined.contains("vfio-pci,host=03:00.0,rombar=0"));
        // Sibling functions attach plain — only the VGA function has the
        // problematic option ROM.
        assert!(joined.contains("-device vfio-pci,host=03:00.1 "));
        assert!(joined.contains("-device vfio-pci,host=03:00.2 "));
        assert!(joined.contains("-device vfio-pci,host=03:00.3 "));
    }

    #[test]
    fn build_vng_boot_command_includes_module_overlay() {
        // vng only looks for modules inside the rootfs when --root is used
        // (virtme run.py: kernel.moddir = "{root}/lib/modules/{kver}"), so
        // the build-tree modules must be overlaid via --rodir or modprobe
        // amdgpu fails inside the guest.
        let config = test_vm_config();
        let manager = VmManager::new(config);
        let overlay = "/lib/modules/7.1.0-rc7+=/k/.virtme_mods/lib/modules/7.1.0-rc7+";
        let cmd = manager.build_boot_command("/ignored", &gpu_functions(), Some(overlay));
        let pos = cmd
            .iter()
            .position(|a| a == overlay)
            .expect("module overlay rodir arg present");
        assert_eq!(cmd[pos - 1], "--rodir");
    }

    #[test]
    fn build_vng_boot_command_exec_runs_guest_agent() {
        let config = test_vm_config();
        let manager = VmManager::new(config);
        let cmd = manager.build_boot_command("/ignored", &gpu_functions(), None);
        let exec_pos = cmd.iter().position(|a| a == "--exec").unwrap();
        let exec_arg = &cmd[exec_pos + 1];
        assert!(exec_arg.contains("guest.crucible_guest_agent"));
        assert!(exec_arg.contains("/opt/crucible"));
    }

    #[test]
    fn vm_state_transitions() {
        let config = test_vm_config();
        let manager = VmManager::new(config);
        assert!(matches!(manager.state(), VmState::Stopped));
    }

    #[test]
    fn build_vng_boot_command_skips_vfio_when_unset() {
        let toml_str = r#"
            kernel_src = "/k"
            guest_rootfs = "/r"
            vfio_device = ""
        "#;
        let config: crate::config::VmConfig = toml::from_str(toml_str).unwrap();
        let manager = VmManager::new(config);
        let cmd = manager.build_boot_command("/path/to/bzImage", &[], None);
        let joined = cmd.join(" ");
        assert!(!joined.contains("vfio-pci"), "joined cmd: {}", joined);
        assert!(joined.contains("vhost-vsock-pci"));
    }

    #[test]
    fn build_vng_boot_command_skips_vfio_when_none() {
        let toml_str = r#"
            kernel_src = "/k"
            guest_rootfs = "/r"
            vfio_device = "none"
        "#;
        let config: crate::config::VmConfig = toml::from_str(toml_str).unwrap();
        let manager = VmManager::new(config);
        let cmd = manager.build_boot_command("/path/to/bzImage", &[], None);
        let joined = cmd.join(" ");
        assert!(!joined.contains("vfio-pci"), "joined cmd: {}", joined);
    }

    #[test]
    fn validate_passthrough_noop_when_disabled() {
        for dev in ["", "none", "None", "  "] {
            let called = std::cell::Cell::new(false);
            let result = VmManager::validate_passthrough_with(dev, |_| {
                called.set(true);
                None
            });
            assert!(result.is_ok(), "dev {dev:?} should skip validation");
            assert!(!called.get(), "dev {dev:?} should not read sysfs");
        }
    }

    #[test]
    fn validate_passthrough_accepts_vfio_pci_binding() {
        let result = VmManager::validate_passthrough_with("0a:00.0", |addr| {
            assert_eq!(addr, "0000:0a:00.0"); // short form gets domain-qualified
            Some("vfio-pci".to_string())
        });
        assert!(result.is_ok());
    }

    #[test]
    fn validate_passthrough_rejects_wrong_driver() {
        let result = VmManager::validate_passthrough_with("0000:0a:00.0", |addr| {
            assert_eq!(addr, "0000:0a:00.0"); // full form passes through
            Some("amdgpu".to_string())
        });
        let err = result.unwrap_err().to_string();
        assert!(err.contains("amdgpu"), "err: {err}");
        assert!(err.contains("setup-host.sh"), "err: {err}");
    }

    #[test]
    fn validate_passthrough_rejects_unbound_device() {
        let result = VmManager::validate_passthrough_with("0a:00.0", |_| None);
        let err = result.unwrap_err().to_string();
        assert!(err.contains("no driver bound"), "err: {err}");
    }

    #[test]
    fn sibling_functions_discovered_and_sorted() {
        let funcs = VmManager::vfio_sibling_functions_with("03:00.0", |prefix| {
            assert_eq!(prefix, "0000:03:00.");
            vec![
                "0000:03:00.2".to_string(),
                "0000:03:00.0".to_string(),
                "0000:03:00.3".to_string(),
                "0000:03:00.1".to_string(),
            ]
        });
        assert_eq!(funcs, vec!["03:00.0", "03:00.1", "03:00.2", "03:00.3"]);
    }

    #[test]
    fn sibling_functions_falls_back_to_configured_device() {
        // If sysfs can't be listed (tests, exotic setups), at least the
        // configured function must be passed.
        let funcs = VmManager::vfio_sibling_functions_with("0a:00.0", |_| vec![]);
        assert_eq!(funcs, vec!["0a:00.0"]);
    }

    #[test]
    fn validate_passthrough_all_rejects_unbound_sibling() {
        // The audio/USB/UCSI functions live in their own IOMMU groups; a
        // GPU bus reset touches them all, so every sibling must be on
        // vfio-pci before QEMU spawns.
        let result = VmManager::validate_passthrough_functions_with(
            &["03:00.0".to_string(), "03:00.1".to_string()],
            |addr| match addr {
                "0000:03:00.0" => Some("vfio-pci".to_string()),
                _ => Some("snd_hda_intel".to_string()),
            },
        );
        let err = result.unwrap_err().to_string();
        assert!(err.contains("03:00.1"), "err: {err}");
        assert!(err.contains("snd_hda_intel"), "err: {err}");
    }

    #[test]
    fn module_overlay_matches_kernel_release_exactly() {
        // .virtme_mods accumulates stale trees (an old "7.1.0-rc7+" next
        // to the current "7.1.0-rc7-virtme" after vng --build changed the
        // localversion); modprobe consults /lib/modules/$(uname -r), so
        // the overlay must come from include/config/kernel.release, not
        // from whichever dir readdir happens to return first.
        let tmp = tempfile::tempdir().unwrap();
        let mods = tmp.path().join(".virtme_mods/lib/modules");
        std::fs::create_dir_all(mods.join("0.0.0")).unwrap();
        std::fs::create_dir_all(mods.join("7.1.0-rc7+")).unwrap(); // stale
        std::fs::create_dir_all(mods.join("7.1.0-rc7-virtme")).unwrap();
        std::fs::create_dir_all(tmp.path().join("include/config")).unwrap();
        std::fs::write(
            tmp.path().join("include/config/kernel.release"),
            "7.1.0-rc7-virtme\n",
        )
        .unwrap();
        let overlay = VmManager::find_module_overlay(tmp.path().to_str().unwrap())
            .expect("overlay for the running release");
        assert!(overlay.starts_with("/lib/modules/7.1.0-rc7-virtme="));
        assert!(overlay.ends_with("/.virtme_mods/lib/modules/7.1.0-rc7-virtme"));
    }

    #[test]
    fn module_overlay_none_when_release_tree_missing() {
        // kernel.release exists but modules_install never ran for it.
        let tmp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(tmp.path().join("include/config")).unwrap();
        std::fs::write(
            tmp.path().join("include/config/kernel.release"),
            "7.1.0-rc7-virtme\n",
        )
        .unwrap();
        assert!(VmManager::find_module_overlay(tmp.path().to_str().unwrap()).is_none());
    }

    #[test]
    fn module_overlay_none_without_mods_dir() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(VmManager::find_module_overlay(tmp.path().to_str().unwrap()).is_none());
    }
}
