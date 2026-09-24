// SPDX-License-Identifier: Apache-2.0
//
// WSL2 compatibility shim for PyTorch expandable CUDA segments.
//
// Some WSL2 GPU stacks report that GPUDirect RDMA is supported for CUDA VMM
// allocations, but fail when PyTorch makes the resulting mapping accessible.
// PyTorch resolves cuMemCreate through cudaGetDriverEntryPointByVersion, so the
// shim replaces only that returned entry point and clears the RDMA capability
// bit before forwarding the call. It is loaded only by compose.wsl2.yaml.

#define _GNU_SOURCE

#include <cuda.h>
#include <cuda_runtime_api.h>
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>

typedef cudaError_t (*get_entry_point_fn)(
    const char*, void**, unsigned int, unsigned long long,
    enum cudaDriverEntryPointQueryResult*);
typedef CUresult (*mem_create_fn)(
    CUmemGenericAllocationHandle*, size_t, const CUmemAllocationProp*,
    unsigned long long);

static mem_create_fn real_mem_create;

static CUresult mem_create_without_rdma(
    CUmemGenericAllocationHandle* handle,
    size_t size,
    const CUmemAllocationProp* prop,
    unsigned long long flags) {
  if (prop == NULL || real_mem_create == NULL) {
    return CUDA_ERROR_INVALID_VALUE;
  }
  CUmemAllocationProp modified = *prop;
  const unsigned int previous = modified.allocFlags.gpuDirectRDMACapable;
  modified.allocFlags.gpuDirectRDMACapable = 0;
  fprintf(
      stderr,
      "VMM_RDMA_INTERPOSE cuMemCreate size=%zu rdma=%u->0\n",
      size,
      previous);
  return real_mem_create(handle, size, &modified, flags);
}

cudaError_t cudaGetDriverEntryPointByVersion(
    const char* symbol,
    void** func_ptr,
    unsigned int cuda_version,
    unsigned long long flags,
    enum cudaDriverEntryPointQueryResult* driver_status) {
  static get_entry_point_fn real_get_entry_point;
  if (real_get_entry_point == NULL) {
    real_get_entry_point =
        (get_entry_point_fn)dlsym(RTLD_NEXT, "cudaGetDriverEntryPointByVersion");
    if (real_get_entry_point == NULL) {
      fprintf(stderr, "VMM_RDMA_INTERPOSE failed to resolve runtime entry point\n");
      return cudaErrorSharedObjectSymbolNotFound;
    }
  }

  cudaError_t status = real_get_entry_point(
      symbol, func_ptr, cuda_version, flags, driver_status);
  if (status == cudaSuccess && symbol != NULL && func_ptr != NULL &&
      strcmp(symbol, "cuMemCreate") == 0) {
    real_mem_create = (mem_create_fn)*func_ptr;
    *func_ptr = (void*)mem_create_without_rdma;
    fprintf(stderr, "VMM_RDMA_INTERPOSE replaced cuMemCreate entry point\n");
  }
  return status;
}
