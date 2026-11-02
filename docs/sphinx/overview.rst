Overview
========

Avian Biosurveillance is a passive-acoustic early-warning platform for unusual
changes and mortality in Dutch bird populations, built entirely on public data.
It turns continuous, citizen-science bird-audio recordings into a weekly,
spatially-resolved anomaly signal that helps experts decide which species,
locations, and weeks warrant a closer look — before a change becomes visible in
reported bird mortality.

The platform is a **decision-support tool for expert review, not a
disease-diagnosis system**.

Premise
-------

Sick or stressed birds tend to call less and produce altered songs, so a
sustained, unusual change in a population's acoustic and detection activity can
act as an indirect early-warning signal. Watching bird populations acoustically
can therefore give advance notice of die-offs and other population-level stress
before they show up in mortality reports.

Tracked species
---------------

Eleven species are tracked: seven *target* species (Eurasian Blackbird, Song
Thrush, Dunnock, Eurasian Wren, European Robin, Common Wood-Pigeon, and
Eurasian Blue Tit) and four *control* species in comparable habitats (Eurasian
Blackcap, Willow Warbler, Common Chaffinch, and Western Jackdaw). Species
selection was guided by ornithology domain experts.

Where to go next
----------------

* :doc:`architecture` — services, storage, and how the pieces fit together.
* :doc:`data-pipeline` — data sources, ingestion, quality, and audio processing.
* :doc:`modeling-and-evaluation` — the anomaly signals and how they are evaluated.
* :doc:`deployment` — running the stack and development practices.
