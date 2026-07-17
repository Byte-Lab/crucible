/* thp_always_shim.c — LD_PRELOAD shim: opt this process into aggressive THP
 * via the PR_SET_THP_ALWAYS prctl (mm-thp-always-prctl.diff kernel patch).
 *
 * Build:  gcc -shared -fPIC -O2 -o libthpalways.so thp_always_shim.c
 * Use:    LD_PRELOAD=/path/libthpalways.so ./factorio --benchmark saves/bench.zip ...
 *
 * Verify: grep AnonHugePages /proc/$(pgrep factorio)/smaps_rollup
 *         perf stat -e dTLB-loads,dTLB-load-misses -p $(pgrep factorio) -- sleep 30
 *         cat /sys/kernel/mm/transparent_hugepage/enabled   (stays [madvise])
 */
#define _GNU_SOURCE
#include <sys/prctl.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>

#ifndef PR_SET_THP_ALWAYS
#define PR_SET_THP_ALWAYS 79
#endif
#ifndef PR_GET_THP_ALWAYS
#define PR_GET_THP_ALWAYS 80
#endif

__attribute__((constructor(101)))
static void thp_always_init(void)
{
	if (prctl(PR_SET_THP_ALWAYS, 1, 0, 0, 0) != 0) {
		fprintf(stderr, "[thp-shim] PR_SET_THP_ALWAYS failed: %s "
			"(kernel without the patch?)\n", strerror(errno));
		return;
	}
	fprintf(stderr, "[thp-shim] pid %d: aggressive THP enabled (PR_GET_THP_ALWAYS=%d)\n",
		getpid(), (int)prctl(PR_GET_THP_ALWAYS, 0, 0, 0, 0));
}
