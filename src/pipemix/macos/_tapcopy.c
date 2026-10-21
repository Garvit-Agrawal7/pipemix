/*
 * The IOProc that copies a process tap to a private aggregate's outputs. It runs
 * on Core Audio's real-time thread, where taking the GIL would glitch, so it is C.
 * The tap is the last non-NULL input buffer; the meter lets Python spot a tap that
 * delivers only silence (a missing System Audio Recording permission).
 */

#include <CoreAudio/AudioHardware.h>
#include <string.h>

typedef struct {
    float peak;               /* max |sample| since Python last reset it */
    unsigned long long cycles;
} pm_meter;

static OSStatus pm_copy(AudioObjectID device, const AudioTimeStamp *now,
                        const AudioBufferList *in, const AudioTimeStamp *in_time,
                        AudioBufferList *out, const AudioTimeStamp *out_time,
                        void *ctx)
{
    (void)device; (void)now; (void)in_time; (void)out_time;
    pm_meter *meter = (pm_meter *)ctx;

    const AudioBuffer *src = NULL;
    if (in) {
        for (UInt32 i = in->mNumberBuffers; i-- > 0;) {
            if (in->mBuffers[i].mData && in->mBuffers[i].mNumberChannels) {
                src = &in->mBuffers[i];
                break;
            }
        }
    }

    UInt32 in_ch = src ? src->mNumberChannels : 0;
    UInt32 in_frames = src ? src->mDataByteSize / (UInt32)(sizeof(float) * in_ch) : 0;
    const float *s = src ? (const float *)src->mData : NULL;

    if (meter) {
        float peak = meter->peak;
        for (UInt32 i = 0; i < in_frames * in_ch; i++) {
            float v = s[i] < 0 ? -s[i] : s[i];
            if (v > peak) peak = v;
        }
        meter->peak = peak;
        meter->cycles++;
    }

    if (!out) return noErr;

    /* A running channel count across buffers, so a non-interleaved stereo
     * device (one buffer per channel) still gets left and right. */
    UInt32 channel = 0;
    for (UInt32 b = 0; b < out->mNumberBuffers; b++) {
        AudioBuffer *dst = &out->mBuffers[b];
        if (!dst->mData) continue;
        UInt32 out_ch = dst->mNumberChannels ? dst->mNumberChannels : 1;
        UInt32 frames = dst->mDataByteSize / (UInt32)(sizeof(float) * out_ch);
        float *o = (float *)dst->mData;

        if (!s) {
            memset(o, 0, dst->mDataByteSize);
        } else {
            UInt32 n = frames < in_frames ? frames : in_frames;
            for (UInt32 f = 0; f < n; f++)
                for (UInt32 c = 0; c < out_ch; c++)
                    o[f * out_ch + c] = s[f * in_ch + (channel + c) % in_ch];
            if (n < frames)
                memset(o + n * out_ch, 0, (frames - n) * out_ch * sizeof(float));
        }
        channel += out_ch;
    }
    return noErr;
}

AudioDeviceIOProc pm_copy_proc(void) { return pm_copy; }
