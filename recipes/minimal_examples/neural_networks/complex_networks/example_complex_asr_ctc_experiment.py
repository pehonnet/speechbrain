#!/usr/bin/python
import os
import speechbrain as sb


class CTCBrain(sb.Brain):
    def compute_forward(self, batch, stage):
        wavs, lens = batch.sig
        feats = self.hparams.compute_features(wavs)
        feats = self.modules.mean_var_norm(feats, lens)
        outputs = self.modules.model(feats)

        return outputs, lens

    def compute_objectives(self, predictions, batch, stage):
        predictions, lens = predictions
        phns, phn_lens = batch.phn_encoded
        loss = self.hparams.compute_cost(predictions, phns, lens, phn_lens)

        if stage != sb.Stage.TRAIN:
            seq = sb.decoders.ctc_greedy_decode(predictions, lens, blank_id=-1)
            self.per_metrics.append(batch.id, seq, phns, target_len=phn_lens)

        return loss

    def on_stage_start(self, stage, epoch=None):
        if stage != sb.Stage.TRAIN:
            self.per_metrics = self.hparams.per_stats()

    def on_stage_end(self, stage, stage_loss, epoch=None):
        if stage == sb.Stage.TRAIN:
            self.train_loss = stage_loss
        if stage == sb.Stage.VALID:
            print("Epoch %d complete" % epoch)
            print("Train loss: %.2f" % self.train_loss)
        if stage != sb.Stage.TRAIN:
            print(stage, "loss: %.2f" % stage_loss)
            print(stage, "PER: %.2f" % self.per_metrics.summarize("error_rate"))


def main():
    experiment_dir = os.path.dirname(os.path.realpath(__file__))
    hparams_file = os.path.join(experiment_dir, "hyperparams.yaml")
    data_folder = "../../../../samples/audio_samples/nn_training_samples"
    data_folder = os.path.realpath(os.path.join(experiment_dir, data_folder))
    with open(hparams_file) as fin:
        hparams = sb.load_extended_yaml(fin, {"data_folder": data_folder})

    # Update label encoder:
    label_encoder = hparams["label_encoder"]
    label_encoder.update_from_didataset(
        hparams["train_data"], output_key="phn_list", sequence_input=True
    )
    label_encoder.update_from_didataset(
        hparams["valid_data"], output_key="phn_list", sequence_input=True
    )
    label_encoder.insert_blank(index=hparams["blank_index"])

    ctc_brain = CTCBrain(hparams["modules"], hparams["opt_class"], hparams)
    ctc_brain.fit(
        range(hparams["N_epochs"]),
        hparams["train_data"],
        hparams["valid_data"],
    )
    ctc_brain.evaluate(hparams["valid_data"])

    # Check that model overfits for an integration test
    assert ctc_brain.train_loss < 0.1


if __name__ == "__main__":
    main()


def test_error():
    main()
