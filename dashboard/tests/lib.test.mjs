import assert from "node:assert/strict";
import { test } from "node:test";

import { formatAge, linePoints, projectTrack, scale } from "../lib.js";

test("scale maps the range linearly and a flat range to the middle", () => {
  assert.equal(scale(5, 0, 10, 0, 100), 50);
  assert.equal(scale(0, 0, 10, 100, 0), 100); // inverted output range
  assert.equal(scale(7, 7, 7, 0, 100), 50);
});

test("linePoints draws min at the bottom and max at the top", () => {
  const { points, min, max } = linePoints([0, 10], 100, 50, 0);
  assert.deepEqual([min, max], [0, 10]);
  assert.equal(points, "0.0,50.0 100.0,0.0");
});

test("linePoints skips missing values and survives empty or single series", () => {
  assert.equal(linePoints([], 100, 50).points, "");
  assert.equal(linePoints([null, NaN], 100, 50).points, "");
  assert.equal(linePoints([3], 100, 50, 0).points.split(" ").length, 1);
  assert.equal(linePoints([1, null, 3], 100, 50, 0).points.split(" ").length, 2);
});

test("projectTrack stays inside the box, north is up and east is right", () => {
  const track = [
    { latitude: 49.0, longitude: 12.0 },
    { latitude: 49.01, longitude: 12.02 },
  ];
  const [a, b] = projectTrack(track, 200, 100, 10);
  for (const p of [a, b]) {
    assert.ok(p.x >= 0 && p.x <= 200 && p.y >= 0 && p.y <= 100);
  }
  assert.ok(b.x > a.x, "east is to the right");
  assert.ok(b.y < a.y, "north is up (smaller y)");
});

test("projectTrack corrects longitude by cos(latitude) so shapes are not stretched", () => {
  // 0.01 deg north vs 0.01 deg east at 60 degrees latitude: east is only ~half as far.
  const [origin, north, east] = projectTrack(
    [
      { latitude: 60, longitude: 10 },
      { latitude: 60.01, longitude: 10 },
      { latitude: 60, longitude: 10.01 },
    ],
    200,
    200,
    0,
  );
  const dNorth = Math.abs(origin.y - north.y);
  const dEast = Math.abs(east.x - origin.x);
  assert.ok(Math.abs(dEast / dNorth - 0.5) < 0.02);
});

test("projectTrack handles empty, single point and invalid points", () => {
  assert.deepEqual(projectTrack([], 100, 100), []);
  assert.equal(projectTrack([{ latitude: 1, longitude: 2 }], 100, 100).length, 1);
  assert.equal(projectTrack([{ latitude: null, longitude: 2 }], 100, 100).length, 0);
});

test("formatAge picks a sensible unit", () => {
  const now = Date.parse("2026-11-10T10:00:00Z");
  assert.equal(formatAge("2026-11-10T09:59:48Z", now), "12 s ago");
  assert.equal(formatAge("2026-11-10T09:57:00Z", now), "3 min ago");
  assert.equal(formatAge("2026-11-10T07:00:00Z", now), "3 h ago");
  assert.equal(formatAge("2026-11-08T10:00:00Z", now), "2 d ago");
  assert.equal(formatAge("2026-11-10T10:00:05Z", now), "0 s ago"); // clock skew never negative
});
