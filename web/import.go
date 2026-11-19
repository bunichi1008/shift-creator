package main

import (
	"encoding/csv"
	"fmt"
	"io"
	"slices"
	"strconv"
	"strings"
)

func importCSV(p *Problem, kind, contents string) error {
	headers := map[string][]string{
		"employees":   {"id", "name", "skills", "target_minutes", "max_minutes", "target_nights"},
		"coverage":    {"date", "shift_id", "minimum", "maximum"},
		"preferences": {"employee_id", "date", "kind", "shift_id", "weight"},
	}
	expected, ok := headers[kind]
	if !ok {
		return inputError("取り込むデータの種類を選択してください")
	}
	reader := csv.NewReader(strings.NewReader(strings.TrimPrefix(contents, "\ufeff")))
	header, err := reader.Read()
	if err != nil || !slices.Equal(header, expected) {
		return inputError("CSV の列名を確認してください（" + strings.Join(expected, ", ") + "）")
	}
	employees := []Employee{}
	coverage := []Coverage{}
	preferences := []Preference{}
	for line := 2; ; line++ {
		row, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil || line > 3101 {
			return inputError(fmt.Sprintf("CSV の %d 行目を確認してください", line))
		}
		for i := range row {
			row[i] = strings.TrimSpace(row[i])
		}
		switch kind {
		case "employees":
			target, a := strconv.Atoi(row[3])
			maximum, b := strconv.Atoi(row[4])
			nights, c := strconv.Atoi(row[5])
			if a != nil || b != nil || c != nil {
				return inputError(fmt.Sprintf("CSV の %d 行目の勤務時間・回数は整数で指定してください", line))
			}
			skills := []string{}
			if row[2] != "" {
				skills = strings.Split(row[2], "|")
			}
			employees = append(employees, Employee{
				ID: row[0], Name: row[1], Skills: skills,
				TargetMinutes: target, MaxMinutes: maximum, TargetNights: nights,
			})
		case "coverage":
			minimum, a := strconv.Atoi(row[2])
			maximum, b := strconv.Atoi(row[3])
			if a != nil || b != nil {
				return inputError(fmt.Sprintf("CSV の %d 行目の人数は整数で指定してください", line))
			}
			coverage = append(coverage, Coverage{Date: row[0], ShiftID: row[1], Minimum: minimum, Maximum: maximum})
		case "preferences":
			weight, err := strconv.Atoi(row[4])
			if err != nil {
				return inputError(fmt.Sprintf("CSV の %d 行目の重みは整数で指定してください", line))
			}
			preferences = append(preferences, Preference{
				EmployeeID: row[0], Date: row[1], Kind: row[2], ShiftID: row[3], Weight: weight,
			})
		}
	}
	switch kind {
	case "employees":
		p.Employees = employees
	case "coverage":
		p.Coverage = coverage
	case "preferences":
		p.Preferences = preferences
	}
	return nil
}

func csvValue(value string) string {
	trimmed := strings.TrimLeft(value, " \t\r\n")
	if strings.HasPrefix(value, "\t") || strings.HasPrefix(value, "\r") || strings.HasPrefix(value, "\n") ||
		(len(trimmed) > 0 && strings.ContainsRune("=+-@", rune(trimmed[0]))) {
		return "'" + value
	}
	return value
}
