/* pvr-dri-negotiate: ask the closed DDK support library (libpvr_dri_support.so) which version of the PVR DRI
 * support interface it offers and which versions of the Mesa-side callback interface it accepts. This is the
 * registration handshake that Imagination's Mesa glue runs when its DRI driver loads (TI mesa powervr/24.0.1,
 * src/gallium/frontends/pvr/pvrcompat.c: PVRDRICompatInit -> PVRDRIRegisterVersionedCallbacksV2 -> the library
 * calls back RegisterSupportInterface(interface, version, min_version)). No GPU context is created, so it runs on
 * either kernel. Every combination runs in its own child process (the library keeps global state).
 *
 * The 24.0.1 glue registers callbacks version 5 (min 0) and accepts a support interface of any version as long as
 * its minimum is 0 (versions 0-8 known, higher = treated as 8). The 25.2.8 glue knows 0-10, same minimum rule.
 *
 * Build (on the board): gcc -O1 -Wall -o pvr-dri-negotiate pvr-dri-negotiate.c \
 *                           -I<TI mesa tree>/src/gallium/frontends/pvr -ldl
 * Run:                  LD_LIBRARY_PATH=/opt/pvr/lib ./pvr-dri-negotiate [path to libpvr_dri_support.so]
 */
#include <dlfcn.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#include "dri_support.h"

#define IFACE_END(field) \
   (offsetof(struct PVRDRISupportInterfaceV2, field) + sizeof(((struct PVRDRISupportInterfaceV2 *)0)->field))

static unsigned int got_version = ~0u, got_min = ~0u;
static int got_calls;
static const void *got_iface;

static bool cb_register(const void *iface, unsigned int version, unsigned int min_version)
{
   got_version = version;
   got_min = min_version;
   got_iface = iface;
   got_calls++;
   return true;
}

/* the other callbacks must never run during registration; they say so if they do */
#define CALLED(name) fprintf(stderr, "  (callback %s called during registration)\n", name)
static int cb_get_buffers(struct __DRIdrawableRec *d, unsigned int f, uint32_t *s, void *p, uint32_t m,
                          struct PVRDRIImageList *l) { CALLED("GetBuffers"); return 0; }
static bool cb_create_configs(struct __DRIconfigRec ***c, struct __DRIscreenRec *s, int f, const uint8_t *d,
                              const uint8_t *st, unsigned int n, const unsigned int *db, unsigned int ndb,
                              const uint8_t *ms, unsigned int nms, bool a, bool cdm, bool mrb, int yr, int yc,
                              uint32_t w, uint32_t h) { CALLED("CreateConfigs"); return false; }
static struct __DRIconfigRec **cb_concat_configs(struct __DRIscreenRec *s, struct __DRIconfigRec **a,
                                                 struct __DRIconfigRec **b) { CALLED("ConcatConfigs"); return NULL; }
static bool cb_config_query(const PVRDRIConfig *c, PVRDRIConfigAttrib a, unsigned int *v) { CALLED("ConfigQuery"); return false; }
static __DRIimage *cb_lookup_egl_image(struct __DRIscreenRec *s, void *i, void *p) { CALLED("LookupEGLImage"); return NULL; }
static unsigned int cb_get_capability(struct __DRIscreenRec *s, unsigned int c) { CALLED("GetCapability"); return 0; }
static void cb_flush_front(struct __DRIdrawableRec *d, void *p) { CALLED("FlushFrontBuffer"); }
static int cb_get_display_fd(struct __DRIscreenRec *s, void *p) { CALLED("GetDisplayFD"); return -1; }
static void *cb_ref_handle(struct __DRIdrawableRec *d) { CALLED("DrawableGetReferenceHandle"); return NULL; }
static void cb_add_ref(void *h) { CALLED("DrawableAddReference"); }
static void cb_remove_ref(void *h) { CALLED("DrawableRemoveReference"); }
static void cb_destroy_loader_image_state(const struct __DRIscreenRec *s, void *p) { CALLED("DestroyLoaderImageState"); }

static const struct PVRDRICallbacksV2 callbacks = {
   .v0.RegisterSupportInterface = cb_register,
   .v0.GetBuffers = cb_get_buffers,
   .v0.CreateConfigs = cb_create_configs,
   .v0.ConcatConfigs = cb_concat_configs,
   .v0.ConfigQuery = cb_config_query,
   .v0.LookupEGLImage = cb_lookup_egl_image,
   .v0.GetCapability = cb_get_capability,
   .v1.FlushFrontBuffer = cb_flush_front,
   .v2.GetDisplayFD = cb_get_display_fd,
   .v3.DrawableGetReferenceHandle = cb_ref_handle,
   .v3.DrawableAddReference = cb_add_ref,
   .v3.DrawableRemoveReference = cb_remove_ref,
   .v4.DestroyLoaderImageState = cb_destroy_loader_image_state,
};

/* how many of the function pointers in one part of the offered interface are set */
static void count_part(const char *name, size_t start, size_t end)
{
   const void *const *p = (const void *const *)((const char *)got_iface + start);
   size_t n = (end - start) / sizeof(void *), set = 0;

   for (size_t i = 0; i < n; i++)
      set += p[i] != NULL;
   printf(" %s=%zu/%zu", name, set, n);
}

static void report_interface(void)
{
   /* only read what the offered version covers (at most v8, the newest part the 24.0.1 header knows) */
   unsigned int v = got_version > 8 ? 8 : got_version;

   printf("  functions set per part:");
   count_part("v0", offsetof(struct PVRDRISupportInterfaceV2, v0), IFACE_END(v0));
   if (v >= 1) count_part("v1", offsetof(struct PVRDRISupportInterfaceV2, v1), IFACE_END(v1));
   if (v >= 2) count_part("v2", offsetof(struct PVRDRISupportInterfaceV2, v2), IFACE_END(v2));
   if (v >= 4) count_part("v4", offsetof(struct PVRDRISupportInterfaceV2, v4), IFACE_END(v4));
   if (v >= 5) count_part("v5", offsetof(struct PVRDRISupportInterfaceV2, v5), IFACE_END(v5));
   if (v >= 6) count_part("v6", offsetof(struct PVRDRISupportInterfaceV2, v6), IFACE_END(v6));
   if (v >= 7) count_part("v7", offsetof(struct PVRDRISupportInterfaceV2, v7), IFACE_END(v7));
   if (v >= 8) count_part("v8", offsetof(struct PVRDRISupportInterfaceV2, v8), IFACE_END(v8));
   printf("\n");
}

static void run(const char *lib, unsigned int version, unsigned int min_version, int details)
{
   pid_t pid;
   int status;

   fflush(stdout);
   pid = fork();
   if (pid == 0) {
      bool (*reg)(const void *, unsigned int, unsigned int);
      void *h = dlopen(lib, RTLD_NOW);
      bool ok;

      if (!h) {
         printf("dlopen %s failed: %s\n", lib, dlerror());
         _exit(3);
      }
      reg = (bool (*)(const void *, unsigned int, unsigned int))dlsym(h, "PVRDRIRegisterVersionedCallbacksV2");
      if (!reg) {
         printf("%s has no PVRDRIRegisterVersionedCallbacksV2\n", lib);
         _exit(4);
      }
      ok = reg(&callbacks, version, min_version);
      printf("callbacks v%u (min %u): %-8s", version, min_version, ok ? "ACCEPTED" : "refused");
      if (got_calls)
         printf(" support interface offered: v%u (min %u)%s; 24.0.1 glue %s", got_version, got_min,
                got_calls > 1 ? " (more than once)" : "",
                got_min == 0 ? "accepts it" : "REFUSES it (minimum must be 0)");
      else
         printf(" no support interface offered");
      printf("\n");
      if (got_calls && details)
         report_interface();
      fflush(stdout);
      _exit(ok ? 0 : 1);
   }
   if (pid < 0) {
      perror("fork");
      exit(2);
   }
   waitpid(pid, &status, 0);
   if (WIFSIGNALED(status))
      printf("callbacks v%u (min %u): child killed by signal %d\n", version, min_version, WTERMSIG(status));
}

int main(int argc, char **argv)
{
   const char *lib = argc > 1 ? argv[1] : "libpvr_dri_support.so";

   printf("library: %s\n", lib);
   /* what the 24.0.1 glue registers (pvrdri.c: PVRDRICompatInit(&sDRICallbacksV2, 5, 0)), with the details */
   run(lib, 5, 0, 1);
   /* the whole range: which callback versions this build of the library accepts */
   for (unsigned int v = 0; v <= 8; v++)
      if (v != 5)
         run(lib, v, 0, v == 6);
   for (unsigned int m = 1; m <= 5; m++)
      run(lib, 5, m, 0);
   return 0;
}
