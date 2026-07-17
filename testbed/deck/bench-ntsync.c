// SPDX-License-Identifier: GPL-2.0-only
// Copyright (C) 2026 David Vernet
//
// bench-ntsync.c - micro-benchmark for ntsync (drivers/misc/ntsync) mutex
// lock-handoff latency under CPU contention.
//
// N worker threads ping-pong a single ntsync mutex object. Each thread loops:
//   WAIT_ANY(mutex, owner=gettid())  -> acquire
//   busy-spin ~crit_us               -> critical section
//   MUTEX_UNLOCK(owner=gettid())     -> release
//
// Handoff latency is the time from "releaser about to unlock" to "acquirer's
// wait returned": the releaser stamps CLOCK_MONOTONIC into a shared slot
// immediately before the unlock ioctl; the next acquirer reads that stamp
// immediately after its wait returns and takes the delta. Because the mutex
// serializes holders, a single shared slot correctly pairs each release with
// the acquire it wakes.
//
// Purpose: A/B-test a scheduler change that boosts ntsync lock holders --
// lower/steadier handoff latency under contention is the signal.
//
// Build:
//   gcc -O2 -Wall -o bench-ntsync bench-ntsync.c -lpthread
//
// Exit codes: 0 ok, 1 error, 77 skip (no /dev/ntsync).

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>

// Pull in the real UAPI header when the host has it; otherwise fall back to
// the copied definitions below. Layouts must match drivers/misc/ntsync.
#if defined(__has_include)
#  if __has_include(<linux/ntsync.h>)
#    include <linux/ntsync.h>
#    define HAVE_NTSYNC_H 1
#  endif
#endif

// ---- ntsync UAPI fallback (copied from include/uapi/linux/ntsync.h) --------
#ifndef NTSYNC_IOC_CREATE_MUTEX

struct ntsync_sem_args {
	uint32_t count;
	uint32_t max;
};

struct ntsync_mutex_args {
	uint32_t owner;
	uint32_t count;
};

struct ntsync_event_args {
	uint32_t manual;
	uint32_t signaled;
};

#define NTSYNC_WAIT_REALTIME	0x1

struct ntsync_wait_args {
	uint64_t timeout;
	uint64_t objs;
	uint32_t count;
	uint32_t index;
	uint32_t flags;
	uint32_t owner;
	uint32_t alert;
	uint32_t pad;
};

#define NTSYNC_MAX_WAIT_COUNT 64

#define NTSYNC_IOC_CREATE_SEM		_IOW ('N', 0x80, struct ntsync_sem_args)
#define NTSYNC_IOC_WAIT_ANY		_IOWR('N', 0x82, struct ntsync_wait_args)
#define NTSYNC_IOC_WAIT_ALL		_IOWR('N', 0x83, struct ntsync_wait_args)
#define NTSYNC_IOC_CREATE_MUTEX		_IOW ('N', 0x84, struct ntsync_mutex_args)
#define NTSYNC_IOC_CREATE_EVENT		_IOW ('N', 0x87, struct ntsync_event_args)

#define NTSYNC_IOC_SEM_RELEASE		_IOWR('N', 0x81, uint32_t)
#define NTSYNC_IOC_MUTEX_UNLOCK		_IOWR('N', 0x85, struct ntsync_mutex_args)
#define NTSYNC_IOC_MUTEX_KILL		_IOW ('N', 0x86, uint32_t)
#define NTSYNC_IOC_EVENT_SET		_IOR ('N', 0x88, uint32_t)
#define NTSYNC_IOC_EVENT_RESET		_IOR ('N', 0x89, uint32_t)
#define NTSYNC_IOC_EVENT_PULSE		_IOR ('N', 0x8a, uint32_t)
#define NTSYNC_IOC_SEM_READ		_IOR ('N', 0x8b, struct ntsync_sem_args)
#define NTSYNC_IOC_MUTEX_READ		_IOR ('N', 0x8c, struct ntsync_mutex_args)
#define NTSYNC_IOC_EVENT_READ		_IOR ('N', 0x8d, struct ntsync_event_args)

#endif /* NTSYNC_IOC_CREATE_MUTEX */
// ---------------------------------------------------------------------------

#define NSEC_PER_SEC   1000000000ULL
#define NSEC_PER_USEC  1000ULL

#define DEFAULT_THREADS   4
#define DEFAULT_DURATION  10
#define DEFAULT_CRIT_US   50
#define SAMPLES_INIT_CAP  (1u << 16)

// Shared state driving the ping-pong.
static int g_dev_fd = -1;
static int g_mutex_fd = -1;
static atomic_int g_stop = 0;
// CLOCK_MONOTONIC ns stamped by a releaser just before unlock; 0 = none yet.
static atomic_uint_least64_t g_release_ts = 0;

static uint64_t g_crit_ns = DEFAULT_CRIT_US * NSEC_PER_USEC;

// CPU affinity list (may be empty -> no pinning).
static int g_cpus[CPU_SETSIZE];
static int g_ncpus = 0;

static inline uint64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * NSEC_PER_SEC + (uint64_t)ts.tv_nsec;
}

static inline pid_t gettid_(void)
{
	return (pid_t)syscall(SYS_gettid);
}

static void die(const char *msg)
{
	fprintf(stderr, "FATAL: %s: %s\n", msg, strerror(errno));
	exit(1);
}

// Per-thread result accumulator (no false sharing between threads).
struct worker {
	pthread_t tid;
	int index;             // logical worker index (for cpu round-robin)
	uint32_t owner;        // gettid(), nonzero unique mutex owner id
	uint64_t acquisitions; // total successful acquires
	uint64_t *samples;     // handoff latencies in ns
	size_t nsamples;
	size_t cap;
};

static void samples_push(struct worker *w, uint64_t v)
{
	if (w->nsamples == w->cap) {
		size_t ncap = w->cap ? w->cap * 2 : SAMPLES_INIT_CAP;
		uint64_t *p = realloc(w->samples, ncap * sizeof(*p));
		if (!p) {
			fprintf(stderr, "FATAL: out of memory growing sample buffer\n");
			exit(1);
		}
		w->samples = p;
		w->cap = ncap;
	}
	w->samples[w->nsamples++] = v;
}

// Acquire the single mutex on behalf of this thread's owner id.
// Retries on EINTR; any other failure is fatal.
static void mutex_acquire(uint32_t owner)
{
	int32_t objs[1] = { g_mutex_fd };
	for (;;) {
		struct ntsync_wait_args args;
		memset(&args, 0, sizeof(args));
		args.timeout = UINT64_MAX;            // infinite wait
		args.objs = (uint64_t)(uintptr_t)objs;
		args.count = 1;
		args.owner = owner;                   // nonzero => acquire mutex
		int ret = ioctl(g_dev_fd, NTSYNC_IOC_WAIT_ANY, &args);
		if (ret == 0)
			return;
		if (errno == EINTR)
			continue;
		die("NTSYNC_IOC_WAIT_ANY");
	}
}

// Release the mutex held by this thread's owner id (count 1 -> 0).
static void mutex_release(uint32_t owner)
{
	struct ntsync_mutex_args args;
	memset(&args, 0, sizeof(args));
	args.owner = owner;
	if (ioctl(g_mutex_fd, NTSYNC_IOC_MUTEX_UNLOCK, &args) != 0)
		die("NTSYNC_IOC_MUTEX_UNLOCK");
}

static void pin_self(int logical_index)
{
	if (g_ncpus == 0)
		return;
	int cpu = g_cpus[logical_index % g_ncpus];
	cpu_set_t set;
	CPU_ZERO(&set);
	CPU_SET(cpu, &set);
	if (pthread_setaffinity_np(pthread_self(), sizeof(set), &set) != 0) {
		fprintf(stderr, "FATAL: pthread_setaffinity_np(cpu=%d): %s\n",
			cpu, strerror(errno));
		exit(1);
	}
}

static void *worker_main(void *arg)
{
	struct worker *w = arg;
	w->owner = (uint32_t)gettid_();
	if (w->owner == 0) {
		fprintf(stderr, "FATAL: gettid() returned 0\n");
		exit(1);
	}
	pin_self(w->index);

	while (!atomic_load_explicit(&g_stop, memory_order_relaxed)) {
		mutex_acquire(w->owner);

		// Read the stamp published by the previous releaser, as soon
		// after wait-return as possible.
		uint64_t acq = now_ns();
		uint64_t rel = atomic_load_explicit(&g_release_ts,
						    memory_order_acquire);

		// Drain-on-stop: release and exit without doing more work.
		if (atomic_load_explicit(&g_stop, memory_order_relaxed)) {
			mutex_release(w->owner);
			break;
		}

		w->acquisitions++;
		if (rel != 0 && acq > rel)
			samples_push(w, acq - rel);

		// Critical section: busy-spin ~crit_ns holding the mutex.
		uint64_t spin_end = now_ns() + g_crit_ns;
		while (now_ns() < spin_end)
			; // busy

		// Stamp immediately before unlock so the delta captures the
		// full handoff (unlock ioctl + wakeup + wait return).
		atomic_store_explicit(&g_release_ts, now_ns(),
				      memory_order_release);
		mutex_release(w->owner);
	}
	return NULL;
}

static int cmp_u64(const void *a, const void *b)
{
	uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
	return (x > y) - (x < y);
}

// Nearest-rank percentile over a sorted array. `permille` is the percentile
// scaled by 10 (e.g. 500 = p50, 999 = p99.9, 1000 = max) so we can compute
// ceil() with integer math and avoid a libm dependency.
static uint64_t pct_ns(const uint64_t *sorted, size_t n, unsigned permille)
{
	if (n == 0)
		return 0;
	// rank = ceil(permille * n / 1000)
	uint64_t rank = ((uint64_t)permille * (uint64_t)n + 999) / 1000;
	if (rank == 0)
		rank = 1;
	if (rank > n)
		rank = n;
	return sorted[rank - 1];
}

static void usage(const char *prog)
{
	fprintf(stderr,
		"Usage: %s [-t threads] [-d seconds] [-c cpu_list] [-s crit_us]\n"
		"  -t N        worker threads (default %d)\n"
		"  -d S        run duration in seconds (default %d)\n"
		"  -c LIST     comma-separated CPU list to pin threads round-robin\n"
		"              (e.g. -c 0,1,2,3,16,17,18,19); default: no pinning\n"
		"  -s US       critical-section busy-spin in microseconds (default %d)\n"
		"  -h          this help\n",
		prog, DEFAULT_THREADS, DEFAULT_DURATION, DEFAULT_CRIT_US);
}

static void parse_cpu_list(const char *s)
{
	char *buf = strdup(s);
	if (!buf) {
		fprintf(stderr, "FATAL: strdup\n");
		exit(1);
	}
	g_ncpus = 0;
	for (char *tok = strtok(buf, ","); tok; tok = strtok(NULL, ",")) {
		while (*tok == ' ')
			tok++;
		if (*tok == '\0')
			continue;
		char *end = NULL;
		long v = strtol(tok, &end, 10);
		if (end == tok || v < 0 || v >= CPU_SETSIZE) {
			fprintf(stderr, "FATAL: bad cpu in list: '%s'\n", tok);
			exit(1);
		}
		if (g_ncpus >= CPU_SETSIZE) {
			fprintf(stderr, "FATAL: too many cpus in list\n");
			exit(1);
		}
		g_cpus[g_ncpus++] = (int)v;
	}
	free(buf);
	if (g_ncpus == 0) {
		fprintf(stderr, "FATAL: empty cpu list\n");
		exit(1);
	}
}

int main(int argc, char **argv)
{
	int nthreads = DEFAULT_THREADS;
	int duration_s = DEFAULT_DURATION;
	long crit_us = DEFAULT_CRIT_US;

	int opt;
	while ((opt = getopt(argc, argv, "t:d:c:s:h")) != -1) {
		switch (opt) {
		case 't':
			nthreads = atoi(optarg);
			break;
		case 'd':
			duration_s = atoi(optarg);
			break;
		case 'c':
			parse_cpu_list(optarg);
			break;
		case 's':
			crit_us = atol(optarg);
			break;
		case 'h':
			usage(argv[0]);
			return 0;
		default:
			usage(argv[0]);
			return 1;
		}
	}

	if (nthreads < 1) {
		fprintf(stderr, "FATAL: threads must be >= 1\n");
		return 1;
	}
	if (duration_s < 1) {
		fprintf(stderr, "FATAL: duration must be >= 1s\n");
		return 1;
	}
	if (crit_us < 0) {
		fprintf(stderr, "FATAL: crit_us must be >= 0\n");
		return 1;
	}
	g_crit_ns = (uint64_t)crit_us * NSEC_PER_USEC;

	g_dev_fd = open("/dev/ntsync", O_RDWR | O_CLOEXEC);
	if (g_dev_fd < 0) {
		if (errno == ENOENT || errno == ENODEV || errno == ENXIO) {
			printf("SKIP: no /dev/ntsync\n");
			return 77;
		}
		die("open /dev/ntsync");
	}

	// Create ONE mutex object, initially unlocked/unowned.
	struct ntsync_mutex_args margs;
	memset(&margs, 0, sizeof(margs));
	margs.owner = 0;
	margs.count = 0;
	g_mutex_fd = ioctl(g_dev_fd, NTSYNC_IOC_CREATE_MUTEX, &margs);
	if (g_mutex_fd < 0)
		die("NTSYNC_IOC_CREATE_MUTEX");

	struct worker *workers = calloc((size_t)nthreads, sizeof(*workers));
	if (!workers) {
		fprintf(stderr, "FATAL: calloc workers\n");
		return 1;
	}

	uint64_t t_start = now_ns();
	for (int i = 0; i < nthreads; i++) {
		workers[i].index = i;
		if (pthread_create(&workers[i].tid, NULL, worker_main,
				   &workers[i]) != 0)
			die("pthread_create");
	}

	// Let the ping-pong run for the requested duration.
	struct timespec sleep_ts = {
		.tv_sec = duration_s,
		.tv_nsec = 0,
	};
	while (nanosleep(&sleep_ts, &sleep_ts) != 0 && errno == EINTR)
		; // resume remaining sleep on signal

	atomic_store_explicit(&g_stop, 1, memory_order_relaxed);

	for (int i = 0; i < nthreads; i++)
		pthread_join(workers[i].tid, NULL);
	uint64_t t_end = now_ns();

	// Merge samples and totals.
	uint64_t total_acq = 0;
	size_t total_samples = 0;
	for (int i = 0; i < nthreads; i++) {
		total_acq += workers[i].acquisitions;
		total_samples += workers[i].nsamples;
	}

	uint64_t *all = NULL;
	if (total_samples) {
		all = malloc(total_samples * sizeof(*all));
		if (!all) {
			fprintf(stderr, "FATAL: malloc merge buffer\n");
			return 1;
		}
		size_t off = 0;
		for (int i = 0; i < nthreads; i++) {
			memcpy(all + off, workers[i].samples,
			       workers[i].nsamples * sizeof(*all));
			off += workers[i].nsamples;
		}
		qsort(all, total_samples, sizeof(*all), cmp_u64);
	}

	double elapsed_s = (double)(t_end - t_start) / (double)NSEC_PER_SEC;
	double acq_per_sec = elapsed_s > 0.0
		? (double)total_acq / elapsed_s : 0.0;

	double mean_us = 0.0;
	if (total_samples) {
		long double sum = 0.0L;
		for (size_t i = 0; i < total_samples; i++)
			sum += (long double)all[i];
		mean_us = (double)(sum / (long double)total_samples)
			/ (double)NSEC_PER_USEC;
	}

	double p50  = (double)pct_ns(all, total_samples, 500)  / NSEC_PER_USEC;
	double p99  = (double)pct_ns(all, total_samples, 990)  / NSEC_PER_USEC;
	double p999 = (double)pct_ns(all, total_samples, 999)  / NSEC_PER_USEC;
	double pmax = (double)pct_ns(all, total_samples, 1000) / NSEC_PER_USEC;

	// Machine-parseable key=value, one stat per line.
	printf("threads=%d\n", nthreads);
	printf("duration_s=%.3f\n", elapsed_s);
	printf("crit_us=%ld\n", crit_us);
	printf("cpu_pinning=%s\n", g_ncpus ? "on" : "off");
	printf("acquisitions_total=%llu\n", (unsigned long long)total_acq);
	printf("acquisitions_per_sec=%.1f\n", acq_per_sec);
	printf("handoff_samples=%zu\n", total_samples);
	printf("handoff_mean_us=%.3f\n", mean_us);
	printf("handoff_p50_us=%.3f\n", p50);
	printf("handoff_p99_us=%.3f\n", p99);
	printf("handoff_p999_us=%.3f\n", p999);
	printf("handoff_max_us=%.3f\n", pmax);

	free(all);
	for (int i = 0; i < nthreads; i++)
		free(workers[i].samples);
	free(workers);
	close(g_mutex_fd);
	close(g_dev_fd);
	return 0;
}
