/*
 * fdbench: measure false sharing between files_struct.file_lock (open/close
 * churn) and fd_array[0..3] (hot __fget_files_rcu reads of low fds) in one
 * multithreaded process.
 *
 * N reader threads: pread(fd_zero, ...) in a loop -- every syscall does
 *   __fget_files_rcu -> reads fdt->fd[fd_zero] which for fd < 64 is
 *   files_struct.fd_array, on the same cacheline as file_lock/next_fd/
 *   open_fds bitmap head.
 * M churn threads: dup2(fd_zero, 60)/close(60) in a loop -- each op takes
 *   file_lock and writes open_fds bitmap word 0 (fd 60 stays below
 *   NR_OPEN_DEFAULT so the fdtable never expands, and slot 60 is far
 *   from the readers' slot in both layouts).
 *
 * Prints reader ops/sec aggregated. Compare stock vs patched kernel
 * (patched = fd_array cacheline-aligned), and churn=0 vs churn=M.
 *
 * gcc -O2 -pthread -o fdbench fdbench.c
 * ./fdbench <readers> <churners> <seconds>
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static atomic_long reader_ops;
static volatile int stop;
static int fd_zero = -1;

static void pin(int cpu)
{
	cpu_set_t s;
	CPU_ZERO(&s);
	CPU_SET(cpu, &s);
	pthread_setaffinity_np(pthread_self(), sizeof(s), &s);
}

struct targ { int cpu; };

static void *reader(void *p)
{
	char buf[8];
	long ops = 0;

	pin(((struct targ *)p)->cpu);
	while (!stop) {
		if (pread(fd_zero, buf, sizeof(buf), 0) < 0)
			perror("pread");
		ops++;
	}
	atomic_fetch_add(&reader_ops, ops);
	return NULL;
}

static void *churner(void *p)
{
	pin(((struct targ *)p)->cpu);
	/*
	 * dup2 to a fixed high-but-<64 fd: writes file_lock + open_fds
	 * bitmap word 0 every iteration WITHOUT touching fd_array slots
	 * near the readers' fd (close(60) does not write next_fd since
	 * 60 > next_fd). Slot 60 sits in a different cacheline than
	 * slots 0-7 in both layouts, so the only reader-visible line
	 * traffic is the file_lock/bitmap line -- which the patch moves
	 * away from fd_array[0..3].
	 */
	while (!stop) {
		if (dup2(fd_zero, 60) == 60)
			close(60);
	}
	return NULL;
}

int main(int argc, char **argv)
{
	int nr = argc > 1 ? atoi(argv[1]) : 8;
	int nc = argc > 2 ? atoi(argv[2]) : 4;
	int secs = argc > 3 ? atoi(argv[3]) : 5;
	int ncpu = sysconf(_SC_NPROCESSORS_ONLN);
	pthread_t th[256];
	struct targ ta[256];
	int t = 0;

	/* /dev/zero lands at fd 3 behind stdio -- a low slot inside fd_array */
	fd_zero = open("/dev/zero", O_RDONLY);
	if (fd_zero < 0 || fd_zero > 3)
		fprintf(stderr, "warn: fd_zero=%d (want <=3)\n", fd_zero);

	for (int i = 0; i < nr; i++, t++) {
		ta[t].cpu = t % ncpu;
		pthread_create(&th[t], NULL, reader, &ta[t]);
	}
	for (int i = 0; i < nc; i++, t++) {
		ta[t].cpu = t % ncpu;
		pthread_create(&th[t], NULL, churner, &ta[t]);
	}
	sleep(secs);
	stop = 1;
	for (int i = 0; i < t; i++)
		pthread_join(th[i], NULL);
	printf("readers=%d churners=%d secs=%d reader_ops_per_sec=%ld\n",
	       nr, nc, secs, atomic_load(&reader_ops) / secs);
	return 0;
}
