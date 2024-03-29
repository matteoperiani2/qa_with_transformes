#!/bin/bash

import sys
import argparse
import json
import string
from enum import Enum, auto
from collections import Counter
from typing import List

from .utils import is_number, create_dirs_for_file


class AnswerType(Enum):
    UNKNOWN = auto()
    SPAN = auto()
    YES_NO = auto()
    FLUENCY = auto()
    COUNTING = auto()
    MULTIPLE_CHOICE = auto()

    def __str__(self):
        return self.name.lower()

    @classmethod
    def list(cls, return_unknown=True):
        return [str(c) for c in cls if return_unknown or c != AnswerType.UNKNOWN]


def annotate_with_answer_type(coqa_data: dict, use_additional_answers=True):
    for item in coqa_data["data"]:
        passage = item["story"]
        questions = item["questions"]
        answers = item["answers"]

        additional_answers = {}
        if use_additional_answers:
            additional_answers = item.get("additional_answers", {})

        norm_passage = normalize_text(passage)
        for question_item, *question_answers in zip(
            questions, answers, *additional_answers.values()
        ):
            annotate_question_with_answer_type(
                norm_passage, question_item, question_answers
            )


def annotate_question_with_answer_type(
    norm_passage: str, question_item: dict, question_answers: List[dict]
):
    question = question_item["input_text"]
    norm_question = normalize_text(question)

    answer_types = []
    for answer_item in question_answers:
        answer_type = annotate_answer_with_answer_type(
            norm_passage, norm_question, answer_item
        )
        answer_types.append(answer_type)
        answer_type = Counter(answer_types).most_common(n=1)[0][0]
    question_item["answer_type"] = str(answer_type)
    return answer_type


def annotate_answer_with_answer_type(
    norm_passage: str, norm_question: str, answer_item: dict
):
    answer = answer_item["input_text"]
    span_start = answer_item["span_start"]
    span_end = answer_item["span_end"]
    answer_type = get_answer_type(
        norm_passage, norm_question, answer, span_start, span_end
    )
    answer_item["answer_type"] = str(answer_type)

    return answer_type


def get_answer_type(
    norm_passage: str, norm_question: str, answer: str, span_start: int, span_end: int
) -> AnswerType:
    answer = normalize_text(answer)

    if (span_start, span_end) == (-1, -1):
        return AnswerType.UNKNOWN

    if answer in ["yes", "no"]:
        return AnswerType.YES_NO

    span = find_answer_span(norm_passage, answer)
    if span is not None:
        return AnswerType.SPAN

    if "or" in norm_question and answer in norm_question:
        return AnswerType.MULTIPLE_CHOICE

    if is_number(answer):
        return AnswerType.COUNTING

    return AnswerType.FLUENCY


def normalize_text(text):
    """Lower text and remove punctuation and extra whitespace."""

    def white_space_fix(text):
        return " ".join(text.split())

    def remove_punc(text):
        exclude = set(string.punctuation)
        return "".join(ch for ch in text if ch not in exclude)

    def lower(text):
        return text.lower()

    return white_space_fix(remove_punc(lower(text)))


def find_answer_span(passage, answer):
    """Find the answer span in the passage."""
    start_idx = passage.find(answer)
    end_idx = start_idx + len(answer)

    if start_idx == -1:
        return None
    return start_idx, end_idx


def annotate_dataset(data_file: str, output_file: str, ignore_additional_answers=False):
    with open(data_file, "r", encoding="utf-8") as f:
        coqa_data = json.load(f)

    annotate_with_answer_type(
        coqa_data, use_additional_answers=not ignore_additional_answers
    )

    create_dirs_for_file(output_file)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(coqa_data, f)

    del coqa_data


def create_readable_dataset(
    data_file: str, output_file: str, ignore_additional_answers=False
):
    with open(data_file, "r", encoding="utf-8") as f:
        annotated_data = json.load(f)

    story_separator = "====================================================\n"
    create_dirs_for_file(output_file)
    with open(output_file, "w", encoding="utf-8") as f:
        for item in annotated_data["data"]:
            source = item["source"]
            id = item["id"]
            story = item["story"]
            questions = item["questions"]
            answers = item["answers"]
            additional_answers = item.get("additional_answers", {})
            if ignore_additional_answers:
                additional_answers = {}

            readable_story = to_readable_story(
                story, questions, answers, additional_answers, source=source, id=id
            )
            f.write(readable_story + "\n" + story_separator)

    del annotated_data


def to_readable_story(
    story, questions, answers, additional_answers={}, source=None, id=None, split=None
) -> str:
    lines = []

    head = _create_readable_head(source, id, split)
    if head != "":
        lines.append(head + "\n")

    lines.append(f"{story}\n")
    for question_item, *question_answers in zip(
        questions, answers, *additional_answers.values()
    ):
        lines.append(f'turn: {question_item["turn_id"]}')
        lines.append(
            f'Q\t\t{question_item["input_text"]} || {question_item["answer_type"]}'
        )
        for answer_item in question_answers:
            lines.append(
                f'A\t\t{answer_item["input_text"]} || {answer_item["span_text"]} || {answer_item["answer_type"]}'
            )
        lines.append("")

    return "\n".join(lines)


def _create_readable_head(source=None, id=None, split=None):
    head = []
    if source:
        head.append(f"source: {source}")
    if id:
        head.append(f"id: {id}")
    if split:
        head.append(f"split: {split}")

    return ", ".join(head)


def parse_args():
    parser = argparse.ArgumentParser(
        "Annotate the provided CoQA dataset with answer types"
    )
    parser.add_argument(
        "--data-file",
        "-i",
        dest="data_file",
        help="CoQA dataset JSON file.",
        required=True,
    )
    parser.add_argument(
        "--output-file",
        "-o",
        dest="output_file",
        help="Write annotated dataset to file.",
        required=True,
    )
    parser.add_argument(
        "--readable-file",
        "-r",
        dest="readable_file",
        help="Write annotated dataset in a readable format to file.",
    )
    parser.add_argument(
        "--ignore-additional-answers",
        dest="ignore_additional_answers",
        action=argparse.BooleanOptionalAction,
    )

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)
    return parser.parse_args()


def main():
    coqa_data = annotate_dataset(
        OPTS.data_file, OPTS.output_file, OPTS.ignore_additional_answers
    )
    if OPTS.readable_file:
        create_readable_dataset(
            OPTS.output_file, OPTS.readable_file, OPTS.ignore_additional_answers
        )


if __name__ == "__main__":
    OPTS = parse_args()
    main()
