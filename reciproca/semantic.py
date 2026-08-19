"""
Reciproca - semantic affinity: how close a candidate's profile reads to the
niche described in the settings. Everything here is arithmetic and text
handling: turning either one into a vector is a separate job, handed in, so the
model can be absent without any of this having an opinion about it.
"""

import hashlib
import json
import os

from reciproca import config
from reciproca.logging_sink import log, logger
from reciproca.markers import (
    FOLLOWING_LABEL_MARKERS,
    MUTUAL_FOLLOWERS_MARKERS,
    PROFILE_BUTTON_LABELS,
)
from reciproca.utils import brief_error, has_marker, parse_labelled_count


def mean_pooled(rows, mask):
    """One vector for a piece of text, from one vector per token.

    The model returns a vector per token; what is wanted is a vector for the whole
    text, and the average of the tokens is what these models are trained to be read
    by. The mask says which positions are real: a batch is padded to a fixed length,
    and averaging the padding in would drag every short bio towards the same place -
    the shorter the bio, the more padding, so the pull would be strongest exactly
    where there is least to go on.

    Pure Python on purpose. It is a few hundred numbers, so the speed is not worth a
    dependency, and this way it is a part that can be checked without the model.
    """
    if not rows:
        return None

    width = len(rows[0])
    total = [0.0] * width
    counted = 0

    for row, keep in zip(rows, mask or []):
        if not keep:
            continue
        counted += 1
        for index, value in enumerate(row):
            total[index] += value

    if not counted:
        return None
    return [value / counted for value in total]


def normalized(vector):
    """The vector at length 1, or None if it has no length to speak of.

    Cosine divides this out anyway, so this is not what makes the comparison work.
    It is what makes a stored vector comparable to one made later without carrying
    the length around, and it keeps the numbers in a range that does not drift.
    """
    if not vector:
        return None

    length = sum(value * value for value in vector) ** 0.5
    if not length:
        return None
    return [value / length for value in vector]


def cosine(left, right):
    """How close two vectors point, from -1 to 1, or None if either says nothing.

    Written out rather than pulled from a library: it is five lines, and a package
    added here is a package to be found again by the Windows build.
    """
    if not left or not right or len(left) != len(right):
        return None

    dot = sum(a * b for a, b in zip(left, right))
    size = (sum(a * a for a in left) ** 0.5) * (sum(b * b for b in right) ** 0.5)
    return dot / size if size else None


def affinity_between(profile_vector, niche_vector):
    """The two vectors as an affinity from 0 to 1, or None if there is not one.

    Opposite directions are floored at 0 rather than kept negative. Below zero the
    number stops meaning "less like the niche" and starts meaning something about
    the model's own geometry, which is not a thing to rank people by, and the whole
    scale is meant to read as "none of it" to "all of it".
    """
    closeness = cosine(profile_vector, niche_vector)
    return None if closeness is None else max(0.0, min(1.0, closeness))


def profile_description(header_text):
    """What a profile says about itself, out of the header's text, or None.

    The header is one run of text with everything in it. Read from a real profile,
    in order: the username, the display name, the three counts, the category, the
    bio, the line about people in common, the buttons, and then the names of the
    highlight covers. So what is wanted is the stretch between the last count and
    the buttons, which is the category and the bio together.

    They are not told apart, and there is nothing lost by that: Instagram's category
    and the bio both say what somebody does, and both are going into the same
    comparison.

    A header with no counts in it is not a header this code understands, and comes
    back as None rather than as a guess. Same for a profile with nothing written
    between the counts and the buttons, which is a profile nobody can judge rather
    than a bad one.
    """
    lines = [line.strip() for line in (header_text or "").splitlines() if line.strip()]

    start = None
    for index, line in enumerate(lines):
        if parse_labelled_count(line, FOLLOWING_LABEL_MARKERS) is not None:
            start = index + 1
            break
    if start is None:
        return None

    described = []
    for line in lines[start:]:
        lowered = line.lower()
        if lowered in PROFILE_BUTTON_LABELS or has_marker(lowered, MUTUAL_FOLLOWERS_MARKERS):
            break
        described.append(line)

    return "\n".join(described) or None


def profile_text(name=None, category=None, bio=None):
    """The parts of a profile worth comparing, as one piece of text, or None.

    The category is Instagram's own label, filled in from a fixed list rather than
    written by hand, so it says what somebody does in the same words every time.
    The bio says it in theirs. The display name is often a real name and says
    nothing, but it is where some people put what they do, so it goes in last.

    None where there is nothing to read. A profile with an empty bio is common and
    perfectly good, and it must come back unscored rather than scored badly: it is
    a profile nobody can judge, not a bad one.
    """
    parts = [str(part).strip() for part in (category, bio, name) if part]
    joined = ". ".join(part for part in parts if part)
    return joined or None


# Where the model lives once it has been fetched, next to everything else the app
# writes. Deleting the folder is how you make it download again.
MODEL_DIR = config.data_path("model")

# A sentence model small enough to run on a CPU while the browser is busy, and
# multilingual because the bios being read are not all in one language. Quantized,
# which is what keeps it near a hundred megabytes rather than several hundred.
#
# Pinned to a revision rather than to a branch: "main" is whatever the author
# pushed last, and a model that changes underneath you changes every score with it,
# silently, with the old numbers still sitting in the queue beside the new ones.
MODEL_REPO = "Xenova/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REVISION = "main"
MODEL_FILES = ("onnx/model_quantized.onnx", "tokenizer.json")

# The longest piece of text handed to the model. A bio is short; this is only here
# so that a profile with a wall of text cannot make one score take a minute.
MODEL_MAX_TOKENS = 128


def model_file(name):
    """Where one of the model's files sits on disk."""
    return os.path.join(MODEL_DIR, name.replace('/', os.sep))


def model_is_downloaded():
    return all(os.path.exists(model_file(name)) for name in MODEL_FILES)


def file_digest(path):
    """The SHA-256 of a file, as hex."""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def download_model(on_progress=None):
    """Fetch the model files. True if they are all there afterwards.

    Downloaded to a temporary name and renamed once complete, so a download cut off
    half way cannot leave a file that looks finished. That matters more than usual
    here: a truncated model does not fail to load, it loads and returns nonsense,
    and nonsense would come out as affinities that look like numbers.

    The digest of each file is written down beside it and checked on every load
    afterwards. That catches a file that changed or got damaged after it arrived.
    It does not check what arrived in the first place against a known good value,
    which would need that value to be known: the digests are logged, so pinning
    them later is a matter of reading them out of the log.
    """
    import urllib.request

    os.makedirs(MODEL_DIR, exist_ok=True)
    digests = {}

    for name in MODEL_FILES:
        target = model_file(name)
        if os.path.exists(target):
            digests[name] = file_digest(target)
            continue

        url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}"
        partial = target + ".part"
        os.makedirs(os.path.dirname(target), exist_ok=True)

        log(f"⬇️ Fetching {name} for the semantic ranking, this happens once", 'info')
        try:
            def report(blocks, block_size, total):
                if on_progress and total > 0:
                    on_progress(name, min(blocks * block_size, total), total)

            urllib.request.urlretrieve(url, partial, reporthook=report)
            os.replace(partial, target)
            digests[name] = file_digest(target)
        except Exception as e:
            log(f"❌ Could not fetch {name}: {brief_error(e)}", 'error')
            logger.debug(f"Model download failed for {url}", exc_info=True)
            if os.path.exists(partial):
                os.remove(partial)
            return False

    try:
        with open(model_file("digests.json"), 'w', encoding='utf-8') as handle:
            json.dump(digests, handle, indent=2)
    except Exception as e:
        logger.debug(f"Could not record the model digests: {e}")

    for name, digest in digests.items():
        logger.info(f"Model file {name} sha256 {digest}")

    log("✅ Model ready", 'success')
    return True


def model_files_unchanged():
    """True if every file still matches what was written down when it arrived."""
    try:
        with open(model_file("digests.json"), 'r', encoding='utf-8') as handle:
            recorded = json.load(handle)
    except Exception:
        return True  # Nothing to check against is not the same as a mismatch

    for name, digest in recorded.items():
        path = model_file(name)
        if os.path.exists(path) and file_digest(path) != digest:
            log(f"⚠️ {name} is not the file that was downloaded - ignoring the model", 'warning')
            return False
    return True


class SemanticModel:
    """Turns a piece of text into a vector, if it can.

    Absent is a normal state, not an error. The packages may not be installed, the
    download may have failed, the machine may be offline. Every one of those ends
    with available() saying no and the app carrying on exactly as it did before any
    of this existed - a queue ranked on sighting counts alone, which is what it was
    ranked on for its whole life so far.

    Loaded on first use rather than at startup: it costs a second and a couple of
    hundred megabytes, and most runs of the app never follow anybody.
    """

    def __init__(self):
        self.session = None
        self.tokenizer = None
        self.numpy = None
        self.failed = False

    def load(self):
        """Bring the model up. True if it is usable afterwards."""
        if self.session is not None:
            return True
        if self.failed:
            return False

        try:
            import numpy
            import onnxruntime
            from tokenizers import Tokenizer
        except ImportError as e:
            log(f"ℹ️ Semantic ranking is off: {brief_error(e)}", 'info')
            logger.info("Install onnxruntime and tokenizers to turn it on")
            self.failed = True
            return False

        if not model_is_downloaded() and not download_model():
            self.failed = True
            return False

        if not model_files_unchanged():
            self.failed = True
            return False

        try:
            self.tokenizer = Tokenizer.from_file(model_file("tokenizer.json"))
            self.tokenizer.enable_truncation(max_length=MODEL_MAX_TOKENS)
            self.session = onnxruntime.InferenceSession(
                model_file("onnx/model_quantized.onnx"),
                providers=["CPUExecutionProvider"],
            )
            self.numpy = numpy
        except Exception as e:
            log(f"❌ The model would not load: {brief_error(e)}", 'error')
            logger.exception("Loading the semantic model failed")
            self.failed = True
            return False

        logger.info(f"Semantic model ready, inputs: {[i.name for i in self.session.get_inputs()]}")
        return True

    def available(self):
        return self.load()

    def embed(self, text):
        """One vector for a piece of text, or None if it could not be made.

        Never raises. A profile that cannot be turned into a vector has to come back
        unscored, the same as one with nothing written on it: the pass carries on
        and that candidate keeps their sighting count.
        """
        if not (text or "").strip() or not self.load():
            return None

        try:
            encoded = self.tokenizer.encode(text)
            if not encoded.ids:
                return None

            # Which inputs to hand over is read off the model rather than assumed.
            # Exports of the same model differ over token_type_ids, and a missing
            # input is a hard failure at the first score rather than a wrong number.
            available_inputs = {
                "input_ids": encoded.ids,
                "attention_mask": encoded.attention_mask,
                "token_type_ids": encoded.type_ids,
            }
            feed = {
                spec.name: self.numpy.array([available_inputs[spec.name]], dtype=self.numpy.int64)
                for spec in self.session.get_inputs()
                if spec.name in available_inputs
            }

            outputs = self.session.run(None, feed)
            rows = outputs[0][0].tolist()
        except Exception as e:
            logger.debug(f"Could not embed a piece of text: {e}", exc_info=True)
            return None

        return normalized(mean_pooled(rows, encoded.attention_mask))


semantic_model = SemanticModel()


def make_affinity_scorer(read_profile, embed, niche):
    """A function from username to affinity, for score_queue().

    `read_profile` returns whatever the browser can see of a profile as a
    (name, category, bio) triple; `embed` turns a piece of text into a vector.
    Both are handed in: one needs a browser and the other needs a model, and
    neither belongs in the arithmetic.

    The niche is embedded once here rather than per candidate. It does not change
    while a pass runs, and it is the same cost as a profile every time.

    Returns None if there is nothing to compare against, so a pass with no niche
    written, or no model to be had, scores nobody rather than scoring everybody
    zero.
    """
    if not (niche or "").strip():
        return None

    niche_vector = embed(niche.strip())
    if not niche_vector:
        return None

    def score(username):
        name, category, bio = read_profile(username)
        text = profile_text(name=name, category=category, bio=bio)
        if text is None:
            return None
        return affinity_between(embed(text), niche_vector)

    return score
