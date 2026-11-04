/* Linux-only test wrapper. Adopt and reap descendants after their parent dies. */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: parent_death_harness PROGRAM [ARG ...]\n");
        return 2;
    }
    if (prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == -1) {
        int error = errno;
        perror("PR_SET_CHILD_SUBREAPER");
        return error == EINVAL || error == ENOSYS ? 77 : 1;
    }
    pid_t controller = fork();
    if (controller == -1) {
        perror("fork");
        return 1;
    }
    if (controller == 0) {
        execvp(argv[1], argv + 1);
        perror("execvp");
        _exit(127);
    }
    int result = 1;
    for (;;) {
        int status = 0;
        pid_t reaped = waitpid(-1, &status, 0);
        if (reaped == -1) {
            if (errno == EINTR) {
                continue;
            }
            if (errno == ECHILD) {
                break;
            }
            perror("waitpid");
            return 1;
        }
        if (reaped == controller) {
            result = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
        } else {
            printf("adopted_pid=%ld signal=%d\n", (long)reaped,
                   WIFSIGNALED(status) ? WTERMSIG(status) : 0);
            fflush(stdout);
        }
    }
    return result;
}
