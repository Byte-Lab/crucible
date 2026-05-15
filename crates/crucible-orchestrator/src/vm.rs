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

    pub fn build_boot_command(&self, kernel_path: &str) -> Vec<String> {
        // Skip vfio-pci entirely when no device is configured. Required so
        // the synthetic loop runs on commodity hardware without GPU
        // passthrough; the real game-mode milestone still sets a device.
        let vfio_dev = self.config.vfio_device.trim();
        let vfio_opt = if vfio_dev.is_empty() || vfio_dev.eq_ignore_ascii_case("none") {
            String::new()
        } else {
            format!("-device vfio-pci,host={} ", vfio_dev)
        };
        let qemu_opts = format!(
            "{}-m {} -smp {} -device vhost-vsock-pci,guest-cid={}",
            vfio_opt,
            self.config.memory,
            self.config.cpus,
            self.config.vsock_cid,
        );
        vec![
            "vng".to_string(),
            "--boot".to_string(),
            "--kernel".to_string(),
            kernel_path.to_string(),
            "--root".to_string(),
            self.config.guest_rootfs.clone(),
            "--qemu-opts".to_string(),
            qemu_opts,
        ]
    }

    pub async fn boot(&mut self, kernel_path: &str) -> Result<()> {
        if self.state != VmState::Stopped {
            anyhow::bail!("VM is not stopped (current state: {:?})", self.state);
        }
        self.state = VmState::Booting;

        let cmd_args = self.build_boot_command(kernel_path);
        tracing::info!(kernel = kernel_path, "booting VM");

        let child = Command::new(&cmd_args[0])
            .args(&cmd_args[1..])
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
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
            match vsock_client.health_check().await {
                Ok(resp) => {
                    if let crucible_common::protocol::GuestResponse::Ok { .. } = resp
                    {
                        tracing::info!("VM is ready");
                        return Ok(());
                    }
                }
                Err(_) => {}
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
        let cmd = manager.build_boot_command("/path/to/bzImage");
        assert!(cmd.contains(&"--boot".to_string()));
        assert!(cmd.contains(&"--kernel".to_string()));
        assert!(cmd.contains(&"/path/to/bzImage".to_string()));
    }

    #[test]
    fn build_vng_boot_command_contains_qemu_opts() {
        let config = test_vm_config();
        let manager = VmManager::new(config);
        let cmd = manager.build_boot_command("/path/to/bzImage");
        let joined = cmd.join(" ");
        assert!(joined.contains("vfio-pci,host=03:00.0"));
        assert!(joined.contains("-m 16G"));
        assert!(joined.contains("-smp 8"));
        assert!(joined.contains("vhost-vsock-pci,guest-cid=3"));
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
}
