# 2026 Gaokao mathematics, National I, Problem 15 — FBS experiment

Source problem: Right triangular prism ABC-A1B1C1, angle ACB=90 degrees, AC=BC; D midpoint of AB and E midpoint of AC1. (1) Show DE is parallel to plane BCC1B1. (2) If CC1=2 and the angle between DE and plane ACC1A1 is 45 degrees, compute the distance from DE to plane BCC1B1.

Coordinate reduction: place C=(0,0,0), A=(a,0,0), B=(0,a,0), C1=(0,0,2), a>0. Then D=(a/2,a/2,0), E=(a/2,0,1). The line direction has x-component 0, hence DE is parallel to the plane x=0 and lies outside it. For the angle with the plane y=0, sin²(45°) gives ((a/2)²)/((a/2)²+1)=1/2. The perpendicular distance is a/2; algebra yields a/2=1.

**Verification scope**: This experiment sends the fixed mathematical goals derived above to Lean, not the full spatial Euclidean definitions. The coordinate-model selection and translation from line–plane geometry to these algebraic facts are explained here but NOT yet formalized in Lean. The worker proofs are scripted. Therefore passing this experiment does not mean FBS autonomously solved the unmodified exam question.

Run from this directory after installing elan:
```bash
lake update
lake exe cache get
PYTHONPATH=../../src lake env python ../../scripts/run_gaokao_2026_lean.py
```
No paid model is called; this is a real end-to-end FBS orchestration using scripted worker proof responses and the actual Lean kernel.
