/**
 * @file sample_algorithm.cpp
 * @brief Sorting and searching algorithms.
 *
 * Used as a test fixture for code_editor tool testing.
 */

#include <vector>
#include <algorithm>
#include <functional>
#include <iostream>

namespace algo {

// ---------------------------------------------------------------------------
// Bubble Sort
// ---------------------------------------------------------------------------

template <typename T>
void bubble_sort(std::vector<T>& arr) {
    int n = static_cast<int>(arr.size());
    for (int i = 0; i < n - 1; i++) {
        bool swapped = false;
        for (int j = 0; j < n - i - 1; j++) {
            if (arr[j] > arr[j + 1]) {
                std::swap(arr[j], arr[j + 1]);
                swapped = true;
            }
        }
        if (!swapped) {
            break;  // Already sorted
        }
    }
}

// ---------------------------------------------------------------------------
// Binary Search
// ---------------------------------------------------------------------------

template <typename T>
int binary_search(const std::vector<T>& arr, const T& target) {
    int left = 0;
    int right = static_cast<int>(arr.size()) - 1;

    while (left <= right) {
        int mid = left + (right - left) / 2;

        if (arr[mid] == target) {
            return mid;
        } else if (arr[mid] < target) {
            left = mid + 1;
        } else {
            right = mid - 1;
        }
    }

    return -1;  // Not found
}

// ---------------------------------------------------------------------------
// Linked List
// ---------------------------------------------------------------------------

struct ListNode {
    int value;
    ListNode* next;

    ListNode(int val) : value(val), next(nullptr) {}
};

class LinkedList {
public:
    LinkedList() : head_(nullptr), size_(0) {}

    ~LinkedList() {
        ListNode* current = head_;
        while (current != nullptr) {
            ListNode* next = current->next;
            delete current;
            current = next;
        }
    }

    void push_front(int value) {
        ListNode* node = new ListNode(value);
        node->next = head_;
        head_ = node;
        size_++;
    }

    void push_back(int value) {
        ListNode* node = new ListNode(value);
        if (head_ == nullptr) {
            head_ = node;
        } else {
            ListNode* current = head_;
            while (current->next != nullptr) {
                current = current->next;
            }
            current->next = node;
        }
        size_++;
    }

    bool remove(int value) {
        if (head_ == nullptr) {
            return false;
        }

        if (head_->value == value) {
            ListNode* temp = head_;
            head_ = head_->next;
            delete temp;
            size_--;
            return true;
        }

        ListNode* current = head_;
        while (current->next != nullptr) {
            if (current->next->value == value) {
                ListNode* temp = current->next;
                current->next = temp->next;
                delete temp;
                size_--;
                return true;
            }
            current = current->next;
        }

        return false;
    }

    bool contains(int value) const {
        ListNode* current = head_;
        while (current != nullptr) {
            if (current->value == value) {
                return true;
            }
            current = current->next;
        }
        return false;
    }

    int get_size() const { return size_; }

    void print() const {
        ListNode* current = head_;
        while (current != nullptr) {
            std::cout << current->value;
            if (current->next != nullptr) {
                std::cout << " -> ";
            }
            current = current->next;
        }
        std::cout << std::endl;
    }

private:
    ListNode* head_;
    int size_;
};

} // namespace algo
