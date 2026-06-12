use anyhow::{Context, Result};
use std::path::PathBuf;
use tokio::process::Command;

pub struct KernelBuilder {
    kernel_src: PathBuf,
    known_good: Option<String>,
}

impl KernelBuilder {
    pub fn new(kernel_src: impl Into<PathBuf>) -> Self {
        Self {
            kernel_src: kernel_src.into(),
            known_good: None,
        }
    }

    pub fn known_good_commit(&self) -> Option<&str> {
        self.known_good.as_deref()
    }

    pub fn set_known_good_commit(&mut self, commit: impl Into<String>) {
        self.known_good = Some(commit.into());
    }

    pub fn build_vng_build_command(&self) -> Vec<String> {
        vec!["vng".to_string(), "--build".to_string()]
    }

    pub fn build_apply_patch_command(&self, patch_path: &str) -> Vec<String> {
        vec![
            "git".to_string(),
            "-C".to_string(),
            self.kernel_src.to_string_lossy().to_string(),
            "apply".to_string(),
            patch_path.to_string(),
        ]
    }

    pub fn build_revert_command(&self) -> Vec<String> {
        vec![
            "git".to_string(),
            "-C".to_string(),
            self.kernel_src.to_string_lossy().to_string(),
            "checkout".to_string(),
            "--".to_string(),
            ".".to_string(),
        ]
    }

    pub async fn apply_patch(&self, patch_path: &str) -> Result<()> {
        let args = self.build_apply_patch_command(patch_path);
        let output = Command::new(&args[0])
            .args(&args[1..])
            .output()
            .await
            .with_context(|| format!("failed to apply patch: {}", patch_path))?;
        if !output.status.success() {
            anyhow::bail!(
                "git apply failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        tracing::info!(patch = patch_path, "patch applied");
        Ok(())
    }

    /// `make modules_install` into the build tree's `.virtme_mods`, where
    /// `VmManager::find_module_overlay` picks it up. Without this the guest
    /// cannot modprobe modular drivers (amdgpu for game mode) because vng
    /// with `--root` only resolves modules from inside the rootfs.
    pub fn build_modules_install_command(&self) -> Vec<String> {
        vec![
            "make".to_string(),
            "-j".to_string(),
            "modules_install".to_string(),
            "INSTALL_MOD_PATH=.virtme_mods".to_string(),
        ]
    }

    pub async fn build_kernel(&self) -> Result<PathBuf> {
        let args = self.build_vng_build_command();
        let output = Command::new(&args[0])
            .args(&args[1..])
            .current_dir(&self.kernel_src)
            .output()
            .await
            .context("failed to build kernel")?;
        if !output.status.success() {
            anyhow::bail!(
                "kernel build failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        let args = self.build_modules_install_command();
        let output = Command::new(&args[0])
            .args(&args[1..])
            .current_dir(&self.kernel_src)
            .output()
            .await
            .context("failed to install modules")?;
        if !output.status.success() {
            anyhow::bail!(
                "modules_install failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        let bzimage = self.kernel_src.join("arch/x86/boot/bzImage");
        tracing::info!(path = %bzimage.display(), "kernel built");
        Ok(bzimage)
    }

    pub async fn revert_patch(&self) -> Result<()> {
        let args = self.build_revert_command();
        let output = Command::new(&args[0])
            .args(&args[1..])
            .output()
            .await
            .context("failed to revert patch")?;
        if !output.status.success() {
            anyhow::bail!(
                "git checkout failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        tracing::info!("patch reverted");
        Ok(())
    }

    pub async fn get_current_commit(&self) -> Result<String> {
        let output = Command::new("git")
            .args([
                "-C",
                &self.kernel_src.to_string_lossy(),
                "rev-parse",
                "HEAD",
            ])
            .output()
            .await
            .context("failed to get current commit")?;
        if !output.status.success() {
            anyhow::bail!("git rev-parse failed");
        }
        Ok(String::from_utf8_lossy(&output.stdout)
            .trim()
            .to_string())
    }

    pub async fn apply_and_build(&self, patch_path: &str) -> Result<PathBuf> {
        self.apply_patch(patch_path).await?;
        match self.build_kernel().await {
            Ok(bzimage) => Ok(bzimage),
            Err(build_err) => {
                tracing::warn!(err = %build_err, "build failed, reverting patch");
                self.revert_patch().await?;
                Err(build_err)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_command_generation() {
        let builder = KernelBuilder::new("/home/void/upstream/linux");
        let cmd = builder.build_vng_build_command();
        assert_eq!(cmd[0], "vng");
        assert!(cmd.contains(&"--build".to_string()));
    }

    #[test]
    fn modules_install_targets_virtme_mods() {
        let builder = KernelBuilder::new("/home/void/upstream/linux");
        let cmd = builder.build_modules_install_command();
        assert_eq!(cmd[0], "make");
        assert!(cmd.contains(&"modules_install".to_string()));
        assert!(cmd.contains(&"INSTALL_MOD_PATH=.virtme_mods".to_string()));
    }

    #[test]
    fn patch_state_tracking() {
        let builder = KernelBuilder::new("/tmp/kernel");
        assert!(builder.known_good_commit().is_none());
    }

    #[test]
    fn set_known_good_commit() {
        let mut builder = KernelBuilder::new("/tmp/kernel");
        builder.set_known_good_commit("abc123");
        assert_eq!(builder.known_good_commit(), Some("abc123"));
    }

    #[test]
    fn build_apply_patch_command() {
        let builder = KernelBuilder::new("/home/void/upstream/linux");
        let cmd = builder.build_apply_patch_command("/tmp/patch.diff");
        assert!(cmd.contains(&"git".to_string()));
        assert!(cmd.contains(&"apply".to_string()));
        assert!(cmd.contains(&"/tmp/patch.diff".to_string()));
    }

    #[test]
    fn build_revert_command() {
        let builder = KernelBuilder::new("/home/void/upstream/linux");
        let cmd = builder.build_revert_command();
        assert!(cmd.contains(&"git".to_string()));
        assert!(cmd.contains(&"checkout".to_string()));
    }
}
