import math

import torch
from pyannote.core import Annotation, Segment

from nemo.collections.asr.data.audio_to_diar_label import extract_seg_info_from_rttm
from nemo.collections.asr.data.deep_diarize.utils import assign_frame_level_spk_vector
from nemo.collections.asr.modules import AudioToMelSpectrogramPreprocessor
from nemo.collections.asr.parts.preprocessing import WaveformFeaturizer
from nemo.collections.common.parts.preprocessing.collections import DiarizationSpeechLabel
from nemo.core import Dataset


def _inference_collate_fn(batch):
    packed_batch = list(zip(*batch))
    features, feature_length, fr_targets, targets = packed_batch
    assert len(features) == 1, "Currently inference/validation only supports a batch size of 1."
    return features[0], feature_length[0], torch.cat(fr_targets[0], dim=0), targets[0]


class RTTMDataset(Dataset):
    def __init__(
        self,
        manifest_filepath: str,
        preprocessor: AudioToMelSpectrogramPreprocessor,
        featurizer: WaveformFeaturizer,
        window_stride: float,
        subsampling: int,
        segment_seconds: int,
    ):
        self.collection = DiarizationSpeechLabel(
            manifests_files=manifest_filepath.split(","), emb_dict=None, clus_label_dict=None,
        )
        self.preprocessor = preprocessor
        self.featurizer = featurizer
        self.round_digits = 2
        self.frame_per_sec = int(1 / (window_stride * subsampling))
        self.subsampling = subsampling
        self.segment_seconds = segment_seconds

    def _pyannote_annotations(self, rttm_timestamps):
        stt_list, end_list, speaker_list = rttm_timestamps
        annotation = Annotation()
        for start, end, speaker in zip(stt_list, end_list, speaker_list):
            annotation[Segment(start, end)] = speaker
        return annotation

    def __getitem__(self, index):
        sample = self.collection[index]
        with open(sample.rttm_file) as f:
            rttm_lines = f.readlines()
        if sample.offset is None:
            sample.offset = 0
        # todo: unique ID isn't needed
        rttm_timestamps = extract_seg_info_from_rttm("", rttm_lines)
        annotations = self._pyannote_annotations(rttm_timestamps)
        stt_list, end_list, speaker_list = rttm_timestamps
        total_annotated_duration = max(end_list)
        speakers = sorted(list(set(speaker_list)))
        n_segments = math.ceil((total_annotated_duration - sample.offset) / self.segment_seconds)
        start_offset = sample.offset

        segments, lengths, targets = [], [], []
        for n_segment in range(n_segments):
            fr_level_target = assign_frame_level_spk_vector(
                rttm_timestamps=rttm_timestamps,
                round_digits=self.round_digits,
                frame_per_sec=self.frame_per_sec,
                subsampling=self.subsampling,
                preprocessor=self.preprocessor,
                sample_rate=self.preprocessor._sample_rate,
                start_duration=start_offset,
                end_duration=start_offset + self.segment_seconds,
                speakers=speakers,
            )
            segment = self.featurizer.process(sample.audio_file, offset=start_offset, duration=self.segment_seconds)

            length = torch.tensor(segment.shape[0]).long()

            segment, length = self.preprocessor.get_features(segment.unsqueeze_(0), length.unsqueeze_(0))
            segments.append(segment.transpose(1, 2))
            lengths.append(length)
            targets.append(fr_level_target)
            start_offset += self.segment_seconds
        return segments, lengths, targets, annotations

    def _collate_fn(self, batch):
        return _inference_collate_fn(batch)

    def __len__(self):
        return len(self.collection)