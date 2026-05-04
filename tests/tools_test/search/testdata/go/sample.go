package demo

import (
"errors"
"fmt"
"strings"
)

type GoTargetStruct struct {
	Name        string
	ID          int
	Description string
	Tags        []string
}

type Processor interface {
	Process(v int) (int, error)
	GetName() string
}

type DefaultProcessor struct {
	name string
}

func NewDefaultProcessor(name string) *DefaultProcessor {
	return &DefaultProcessor{name: name}
}

func (p *DefaultProcessor) Process(v int) (int, error) {
	if v < 0 {
		return 0, errors.New("negative value not allowed")
	}
	return v * 2, nil
}

func (p *DefaultProcessor) GetName() string {
	return p.name
}

func GoTargetFunction(v int) int {
	if v == 0 {
		return 0
	}
	
	multiplier := 2
	if v > 100 {
		multiplier = 3
	}
	
	return (v * multiplier) + 10
}

func FormatTarget(target *GoTargetStruct) string {
	if target == nil {
		return ""
	}
	tags := strings.Join(target.Tags, ",")
	return fmt.Sprintf("[%d] %s: %s (%s)", target.ID, target.Name, target.Description, tags)
}
