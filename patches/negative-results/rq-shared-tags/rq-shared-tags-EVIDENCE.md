# request_queue nr_active_requests_shared_tags move - SCRAPPED, negative result (2026-07-16)

## Hypothesis (static pahole scan)
nr_active_requests_shared_tags (atomic RMW per request start/finish when
BLK_MQ_F_TAG_HCTX_SHARED; also atomic_read per tag alloc in hctx_may_queue)
shares cacheline 448-511 with root_blkg (read per bio via blkg_lookup) and
sched_shared_tags/blkcg_pols. Separating them should help shared-tags
configs (multi-LUN host_tagset SCSI: megaraid_sas/mpt3sas/mpi3mr/hisi_sas/
smartpqi/pm8001).

## Key mechanism facts (reviewer-verified, valuable for future work)
- QUEUE_SHARED alone (null_blk shared_tags=1, NVMe multi-ns) accounts in
  hctx->nr_active - NOT the q atomic. The q atomic needs HCTX_SHARED
  (null_blk shared_tag_bitmap=1, scsi host_tagset). First benchmark round
  measured a dead path because of this - reviewer caught it.
- sched_shared_tags is NOT read per dispatch (hot path uses cached
  hctx->sched_tags); blkcg_pols is NOT read per bio. root_blkg IS (root
  cgroup fast path).
- requeue_lock line is NOT quiet: blk-flush takes it per flush sequence,
  blk_mq_kick_requeue_list writes requeue_work.data per kick.

## Measurements (vng 32 vCPU, 7950X, interleaved alternating boots, n=18/side,
## null_blk nr_devices=4 shared_tags=1 shared_tag_bitmap=1 + scsi_debug
## host_tagset=1 max_luns=4 delay=0; fio io_uring randread 32 jobs / psync fsync-write)
Variant 1 - move atomic beside requeue_lock (/tmp/rq-ab-varMove.log):
  nullb_randread +2.74% p=0.07; nullb_mqdeadline -2.12% p=0.06;
  scsi_randread -3.21% p=0.018 REGRESSION; scsi_fsyncwrite -2.52% p=0.015
  REGRESSION. The reviewer-predicted flush/requeue collision is real.
Variant 2 - atomic on fully dedicated cacheline (max benefit bound,
  /tmp/rq-ab-results.log): ALL NULL - nullb +0.17% p=0.42, mqdl -0.07%
  p=0.78, scsi_rr +0.11% p=0.60, scsi_fsync +0.02% p=0.96 (CV 0.6-1.1%).

## Conclusion
Zero recoverable benefit at 1-1.7M IOPS on this machine even with dedicated
placement; naive relocation actively regresses SCSI via requeue-line
collision. Current placement is not demonstrably wrong. Possible residual
case: multi-socket bare metal with a real host_tagset HBA at higher IOPS -
out of reach here. Lane closed.

## Lesson (2nd time today, now a rule)
Static layout scans produce plausible-but-wrong candidates. Before building
patch + full A/B: prototype a targeted microbench proving the sharing costs
something (fd_array had one and won; mmap_lock and this lane did not and
died late). c2c-on-workload or a reader-vs-writer throughput probe first.
