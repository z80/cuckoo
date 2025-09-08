import gc

print("Allocated:", gc.mem_alloc(), "bytes")
print("Free:", gc.mem_free(), "bytes")
