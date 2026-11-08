from compiler.instrumentation.compile import pad_rusty_string_storage


def test_fixed_width_string_reads_have_padded_backing_storage():
    source = '''\
@utf08_literal_1 = private unnamed_addr constant [2 x i8] c" \\00"
define i32 @IP4_DECODE(ptr %input) {
entry:
  %str = alloca [16 x i8], align 1
  %bytes = alloca [16 x i8], align 1
  call void @llvm.memset.p0.i64(ptr align 1 %str, i8 0, i64 16, i1 false)
  call void @llvm.memcpy.p0.p0.i64(ptr align 1 %str, ptr align 1 %input, i64 15, i1 false)
  %value = call i32 @STRING_TO_DWORD(ptr %str)
  ret i32 %value
}
'''
    prepared = pad_rusty_string_storage(source)

    assert '@utf08_literal_1 = private unnamed_addr constant [256 x i8]' in prepared
    assert '%str = alloca [256 x i8], align 1' in prepared
    assert 'ptr align 1 %str, i8 0, i64 256' in prepared
    assert 'ptr align 1 %str, ptr align 1 %input, i64 15' in prepared
    assert '%bytes = alloca [16 x i8], align 1' in prepared
