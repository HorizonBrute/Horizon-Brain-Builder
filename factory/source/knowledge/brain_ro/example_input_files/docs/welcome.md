# Welcome to the example neuron bundle

This document is one of three sample docs staged for the `example_neuron_bundle`
first-run smoke test. It exists so a fresh install can ingest real text and answer a
grounded question with **zero operator authoring**.

## What a neuron bundle is

A *bundle* groups neurons over one document collection (here: `example_docs`).

- An **input neuron** (`input_neuron_example`) reads sources and WRITES vectors.
- An **action neuron** (`action_neuron_cli` / `action_neuron_api`) READS those vectors
  and synthesizes a grounded answer.

## How the answer stays grounded

The action app retrieves the nearest chunks from the collection, then asks a small local
model to answer using only that retrieved context. If you ask "what is a neuron bundle?"
the smoke test should answer from THIS document.
