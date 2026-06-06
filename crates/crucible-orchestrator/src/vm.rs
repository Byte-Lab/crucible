use anyhow::{Context, Result};
use std::time::Duration;
use tokio::process::{Child, Command};

use crate::config::VmConfig;

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

    pub fn build_boot_command(&self, _kernel_path: &str) -> Vec<String> {
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
        let vfio_dev = self.config.vfio_device.trim();
        let mut qemu_opts = String::new();
        if !vfio_dev.is_empty() && !vfio_dev.eq_ignore_ascii_case("none") {
            qemu_opts.push_str(&format!("-device vfio-pci,host={} ", vfio_dev));
        }
        qemu_opts.push_str(&format!(
            "-device vhost-vsock-pci,guest-cid={}",
            self.config.vsock_cid
        ));
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
        args.push("--exec".to_string());
        args.push(guest_cmd);
        args.push(format!("--qemu-opts={}", qemu_opts));
        args
    }

    /// Kernel-source path the vng invocation must be run from.
    pub fn kernel_src(&self) -> &str {
        &self.config.kernel_src
    }

    /// Fail fast if a configured VFIO device is not actually bound to
    /// vfio-pci. A GPU still bound to amdgpu (or absent) hangs the QEMU
    /// boot with no useful diagnostic, so we check sysfs before spawning.
    /// No-op when passthrough is disabled (`vfio_device` empty or "none").
    pub fn validate_passthrough(&self) -> Result<()> {
        Self::validate_passthrough_with(&self.config.vfio_device, |addr| {
            std::fs::read_link(format!("/sys/bus/pci/devices/{addr}/driver"))
                .ok()
                .and_then(|p| p.file_name().map(|s| s.to_string_lossy().into_owned()))
        })
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

        let cmd_args = self.build_boot_command(kernel_path);
        tracing::info!(kernel = kernel_path, cmd = %cmd_args.join(" "), "booting VM");

        let child = Command::new(&cmd_args[0])
            .args(&cmd_args[1..])
            .current_dir(&self.config.kernel_src)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            // If the orchestrator crashes or the test panics, send SIGKILL
            // to vng/QEMU so it doesn't keep CID 3 reserved on the host.
            .kill_on_drop(true)
            .spawn()
            .with_context(|| {
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
                return Ok(());
            }
            tokio::time::sleep(poll_interval).await;
        }
    }

    pub async fn shutdown(&mut self) -> Result<()> {
        if let Some(ref mut child) = self.child {
            tracing::info!("shutting down VM");
            child
                .kill()
                .await
                .context("failed to kill VM process")?;
            child
                .wait()
                .await
                .context("failed to wait for VM process")?;
        }
        self.child = None;
        self.state = VmState::Stopped;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
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

    #[test]
    fn build_vng_boot_command() {
        let config = test_vm_config();
        let manager = VmManager::new(config);
        let cmd = manager.build_boot_command("/ignored");
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
        let cmd = manager.build_boot_command("/ignored");
        let joined = cmd.join(" ");
        assert!(joined.contains("vfio-pci,host=03:00.0"), "joined: {}", joined);
        assert!(joined.contains("vhost-vsock-pci,guest-cid=3"));
        // --qemu-opts must use the `=` form so argparse accepts a value
        // that begins with `-`.
        assert!(joined.contains("--qemu-opts=-device "));
    }

    #[test]
    fn build_vng_boot_command_exec_runs_guest_agent() {
        let config = test_vm_config();
        let manager = VmManager::new(config);
        let cmd = manager.build_boot_command("/ignored");
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
        let cmd = manager.build_boot_command("/path/to/bzImage");
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
        let cmd = manager.build_boot_command("/path/to/bzImage");
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
}
